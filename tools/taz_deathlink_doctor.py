"""
taz_deathlink_doctor.py -- watch what DeathLink would send, and why.

Two signals feed it. Being CAUGHT or DROWNED is a real state the game sets, so
those are certain. Everything else -- falling, being crushed, anything that ends
in a respawn -- is inferred from Taz's position jumping, which is why a warp
door once looked exactly like a fall.

This shows both, live, so a false trigger can be identified from what actually
happened rather than guessed at.

Run it next to Launcher.py with the AP client CLOSED (PINE allows one
connection per slot):

    venv\\Scripts\\python.exe taz_deathlink_doctor.py

    watch     every death it would report, as it happens (default)
    verbose   also print state and position changes that report nothing,
              which is how to catch something that SHOULD have counted
    candidate follow a suspected void-out address alongside the current
              detector, so the two can be compared before trusting it:

                  taz_deathlink_doctor.py candidate --addr 0xA7AF08

    compare   the sharp one. Freeze Taz in a known condition, name it, and
              compare two conditions directly. Sampling reports everything
              that moved; this reports only what differs between idle and
              caught, which is a far shorter list.

    scanobj   watch every byte of Taz's objects and report which ones look
              like a state field. Use this when states shows nothing.

    states    log every state Taz enters, how long it lasted, and whether it
              is one we already know. A falling death almost certainly has a
              state of its own -- drowning and being caught do -- and a state
              survives a level change, which a raw address does not.

Do the thing you suspect. Each reported death prints what triggered it, where
Taz was, and whether it would actually be sent given a default yaml.
"""

import argparse
import math
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run it by path instead:")
    print("      venv\\Scripts\\python.exe taz_deathlink_doctor.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

# A built .apworld in custom_worlds takes precedence over worlds/tazwanted, so
# the tool can be reading last week's code while the source is current. Saying
# which file was loaded turns a baffling silence into an obvious cause.
print(f"\n  world loaded from: {G.__file__}")
_MISSING = [n for n in ("taz_state", "death_tick", "posters_done")
            if not hasattr(G.Game, n)]
if _MISSING:
    print(f"  that copy is out of date -- no {_MISSING[0]}.")
    print(f"  Update it, and delete any stale tazwanted.apworld in "
          f"custom_worlds.")

STATE_NAMES = {
    0x00: "idle", 0x0B: "bite", 0x0C: "spin/cage 1", 0x0D: "spin/cage 2",
    0x0E: "spin/cage 3", 0x10: "smushed", 0x2C: "drown", 0x3A: "gum",
    0x3B: "pepper", 0x54: "caught 2", 0x55: "caught 3", 0x59: "CAUGHT",
}


def compare(g):
    """Find the state by comparing two KNOWN conditions, not by sampling.

    Sampling reports everything that moved, which in a game is most of memory.
    Comparing "Taz is idle" against "Taz is caught" and keeping only what
    differs is far sharper -- and it does not depend on catching the right
    frame.

    Taz's own object is scanned as well as the ones it points at, because the
    state may not be in a child at all.
    """
    SPAN = 0x800
    CHILDREN = [0x1C0, 0x1C8, 0x1CC, 0x1D0, 0x1D4]

    def grab():
        try:
            taz = G.mem.read_u32(G.TAZ_PTR)
        except Exception:
            return None
        if not G.mem.valid_ptr(taz):
            return None
        out = {}
        try:
            out[("taz", 0)] = G.mem.read_bytes(taz, SPAN)
        except Exception:
            pass
        for off in CHILDREN:
            try:
                child = G.mem.read_u32(taz + off)
                if G.mem.valid_ptr(child):
                    out[("child", off)] = G.mem.read_bytes(child, SPAN)
            except Exception:
                pass
        return out

    shots = {}
    print("\n  Put Taz in a condition, name it, and press Enter.")
    print("  Do at least: idle, caught, drown, fall.")
    print("  For velocity: snapshot 'still', then 'running', then 'findf'.")
    print("  Then type 'find' -- it compares every snapshot at once and")
    print("  filters out the transform matrix, which swamps a plain diff.")
    print("  ('diff a b' is still there for the raw comparison.)\n")

    while True:
        try:
            cmd = input("  name (or find / diff a b / list / quit) > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd.lower() in ("quit", "q"):
            break
        if cmd.lower() == "list":
            print(f"    {', '.join(sorted(shots)) or 'nothing yet'}\n")
            continue
        if cmd.lower().startswith(("findf", "ff")):
            # Velocity is a FLOAT that is about zero standing still and large
            # while running. Guessing offsets near position found only matrix
            # rows -- zeroing those flattened the model rather than stopping
            # him, because that whole region is his transform.
            #
            # Needs two snapshots named 'still' and 'running'.
            import struct
            # Takes two names, because the pair that matters changes: the
            # first version compared 'still' against 'running' whatever you
            # asked for, so a running-versus-TNT comparison never happened.
            parts = cmd.split()
            n1, n2 = (parts[1], parts[2]) if len(parts) >= 3 \
                else ("still", "running")
            if n1 not in shots or n2 not in shots:
                print(f"    usage: findf <a> <b>, from {sorted(shots)}\n")
                continue
            a, b = shots[n1], shots[n2]
            hits = []
            for key in a:
                if key not in b:
                    continue
                ra, rb = a[key], b[key]
                for off in range(0, min(len(ra), len(rb)) - 3, 4):
                    try:
                        va = struct.unpack_from("<f", ra, off)[0]
                        vb = struct.unpack_from("<f", rb, off)[0]
                    except Exception:
                        continue
                    # Near zero at rest, meaningfully large in motion, and
                    # not absurd -- which rules out garbage interpreted as a
                    # float.
                    # Either direction: what stops him is large in one and
                    # near zero in the other, and which is which depends on
                    # the order the snapshots were named.
                    big_a = 1.0 < abs(va) < 1e5 and abs(vb) < 0.5
                    big_b = 1.0 < abs(vb) < 1e5 and abs(va) < 0.5
                    if (big_a or big_b) and (key[0] != "taz" or off >= 0xC0):
                        hits.append((key, off, va, vb))
            print(f"\n    floats near zero in one and large in the other,\n"
                  f"    skipping the transform matrix below 0xC0:\n")
            print(f"      {'where':<20} {n1[:10]:>10} {n2[:12]:>12}")
            for key, off, va, vb in sorted(
                    hits, key=lambda h: -abs(h[3]))[:30]:
                where = ("TAZ" if key[0] == "taz" else f"[TAZ+{key[1]:#x}]")
                print(f"      {where + ' + ' + hex(off):<20} "
                      f"{va:>10.2f} {vb:>12.2f}")
            if not hits:
                print("      none -- is 'running' really a moving snapshot?")
            print(f"\n    {len(hits)} candidate(s). Three in a row are a")
            print(f"    velocity vector; try zeroing that offset with")
            print(f"    taz_addr.py --hold.\n")
            continue

        if cmd.lower() in ("find", "f"):
            # Every diff so far was drowned in Taz's transform matrix: the
            # first 0xC0 bytes are position and rotation floats, and he moves
            # between snapshots, so they always differ.
            #
            # A state is not a float. It is a small integer in a whole word --
            # 0x59 stored as 59 00 00 00 -- and it takes a DIFFERENT small
            # value in each condition. That is specific enough to find on its
            # own, so this looks for exactly that shape across every snapshot
            # taken, rather than comparing two at a time.
            if len(shots) < 2:
                print("    take at least two named snapshots first\n")
                continue
            names = sorted(shots)
            keys = set(shots[names[0]])
            for n in names[1:]:
                keys &= set(shots[n])

            hits = []
            for key in sorted(keys):
                length = min(len(shots[n][key]) for n in names)
                start = 0xC0 if key[0] == "taz" else 0
                for off in range(start, length - 3, 4):
                    vals = []
                    ok = True
                    for n in names:
                        raw = shots[n][key]
                        # A small integer in a word: low byte set, the other
                        # three zero.
                        if raw[off + 1] or raw[off + 2] or raw[off + 3]:
                            ok = False
                            break
                        if raw[off] > 0x7F:
                            ok = False
                            break
                        vals.append(raw[off])
                    if ok and len(set(vals)) >= 2:
                        hits.append((key, off, vals))

            print(f"\n    words holding a small integer that differs between "
                  f"conditions:\n")
            print(f"      {'where':<22} " +
                  "  ".join(f"{n[:7]:>7}" for n in names))
            KNOWN = {0x2C: "drown", 0x59: "caught", 0x0C: "spin",
                     0x0D: "spin", 0x0E: "spin", 0x10: "smushed"}
            ranked = sorted(hits, key=lambda h: -len(set(h[2])))
            for key, off, vals in ranked[:40]:
                where = ("TAZ" if key[0] == "taz" else f"[TAZ+{key[1]:#x}]")
                label = f"{where} + {off:#05x}"
                marks = []
                for n, v in zip(names, vals):
                    if v in KNOWN and KNOWN[v] in n:
                        marks.append(f"0x{v:02X} IS {n}")
                mark = ("   <-- " + ", ".join(marks)) if marks else ""
                cells = "  ".join(f"{'0x%02X' % v:>7}" for v in vals)
                print(f"      {label:<22} {cells}{mark}")
            if not hits:
                print("      none -- the state may be wider than a byte, or")
                print("      outside the 0x800 scanned")
            print(f"\n    {len(hits)} candidate(s). The row where the value")
            print(f"    for 'caught' is 0x59 and 'drown' is 0x2C is the "
                  f"state.\n")
            continue

        if cmd.lower().startswith("diff"):
            parts = cmd.split()
            if len(parts) != 3 or parts[1] not in shots or parts[2] not in shots:
                print(f"    usage: diff <a> <b>, from {sorted(shots)}\n")
                continue
            a, b = shots[parts[1]], shots[parts[2]]
            print(f"\n    bytes that differ between "
                  f"{parts[1]} and {parts[2]}:\n")
            n = 0
            for key in a:
                if key not in b:
                    continue
                ra, rb = a[key], b[key]
                where = ("TAZ itself" if key[0] == "taz"
                         else f"[TAZ+{key[1]:#x}]")
                start = 0xC0 if key[0] == "taz" else 0
                for i in range(start, min(len(ra), len(rb))):
                    if ra[i] != rb[i]:
                        print(f"      {where} + {i:#05x}   "
                              f"0x{ra[i]:02X} -> 0x{rb[i]:02X}")
                        n += 1
                        if n >= 200:
                            break
                if n >= 200:
                    break
            print(f"\n    {n} shown, skipping the transform matrix below")
            print(f"    0xC0 -- it always differs and it swamped every")
            print(f"    earlier comparison.")
            print(f"\n    A byte here that also differs between")
            print(f"    idle and drown, with a DIFFERENT value each time, is")
            print(f"    the state.\n")
            continue
        if not cmd:
            continue
        shot = grab()
        if not shot:
            print("    could not read Taz -- is a level loaded?\n")
            continue
        shots[cmd.lower()] = shot
        print(f"    saved '{cmd.lower()}' "
              f"({len(shot)} object(s), {SPAN} bytes each)\n")


def scanobj(g, secs):
    """Find which byte of Taz's objects actually holds the state.

    The chain reads and the byte at +0x200 never moves, so that offset is not
    the live field -- whatever it was when it was first noted. Rather than
    guess another one, watch every byte of the objects hanging off Taz and see
    which ones change as he does things.

    Do something distinctive: spin, get caught, fall. A byte that takes a small
    handful of values, changing exactly when Taz changes, is a state field.
    """
    OBJECTS = [("state", 0x1C0), ("request", 0x1C8), ("costume", 0x1CC),
               ("bonus", 0x1D0)]
    SPAN = 0x400

    print(f"\n  Watching {SPAN} bytes of {len(OBJECTS)} objects for "
          f"{secs:.0f}s.")
    print(f"  Spin, get caught, fall in. Ctrl-C to stop early.\n")

    seen = {}          # (object, offset) -> set of values
    base = {}
    t0 = time.time()
    try:
        while time.time() - t0 < secs:
            time.sleep(0.05)
            try:
                taz = G.mem.read_u32(G.TAZ_PTR)
            except Exception:
                continue
            if not G.mem.valid_ptr(taz):
                continue
            for label, off in OBJECTS:
                try:
                    obj = G.mem.read_u32(taz + off)
                except Exception:
                    continue
                if not G.mem.valid_ptr(obj):
                    continue
                try:
                    raw = G.mem.read_bytes(obj, SPAN)
                except Exception:
                    continue
                prev = base.get(label)
                if prev is None:
                    base[label] = raw
                    continue
                for i in range(min(len(raw), len(prev))):
                    if raw[i] != prev[i]:
                        seen.setdefault((label, i), set()).add(raw[i])
                        seen[(label, i)].add(prev[i])
                base[label] = raw
    except KeyboardInterrupt:
        pass

    # Two-value bytes are flags, and there are always dozens; a state field
    # takes a HANDFUL. Sorting by count alone buried the interesting ones
    # under every flag in the object, so they are separated here.
    #
    # Anything whose values include a number we already know -- 0x2C for
    # drowning, 0x59 for being caught -- is almost certainly it.
    KNOWN_STATES = {0x2C, 0x59, 0x0C, 0x0D, 0x0E, 0x10, 0x3A, 0x3B, 0x54, 0x55}
    offs = dict(OBJECTS)

    def line(label, off, vals):
        pretty = ", ".join(f"0x{v:02X}" for v in sorted(vals))
        hit = KNOWN_STATES & vals
        mark = ("   <-- contains " +
                ", ".join(f"0x{v:02X}" for v in sorted(hit))) if hit else ""
        return (f"    [TAZ+{offs[label]:#x}] + {off:#05x}   "
                f"{len(vals):>2} value(s): {pretty}{mark}")

    strong = {k: v for k, v in seen.items() if 3 <= len(v) <= 16}
    print(f"\n  === most likely a state: three to sixteen values ===\n")
    for (label, off), vals in sorted(strong.items(),
                                     key=lambda kv: (-len(KNOWN_STATES & kv[1]),
                                                     len(kv[1]))):
        print(line(label, off, vals))
    if not strong:
        print("    none -- try again and do more distinct things: spin, get")
        print("    caught, drown, fall, and take a powerup")

    print(f"\n  === flags: exactly two values ===\n")
    two = [(k, v) for k, v in seen.items() if len(v) == 2]
    for (label, off), vals in sorted(two)[:20]:
        print(line(label, off, vals))
    if len(two) > 20:
        print(f"    ... and {len(two) - 20} more")

    print(f"\n  {len(seen)} byte(s) moved in total, {len(strong)} in the "
          f"likely range.\n")


def states(g, secs):
    """Log every state Taz enters, with how long it held.

    The state byte lives at [[TAZ_PTR] + 0x1C0] + 0x200 -- a chain, which is
    why it keeps working when a level reloads and a raw address does not.

    Drowning and being caught each have their own state, so falling very
    likely does too. Anything printed as UNKNOWN is a candidate; the one that
    appears exactly when the void animation plays, and never during a warp, is
    what DeathLink should be reading.
    """
    # Walk the chain out loud first. Printing nothing at all is far more
    # likely to mean the read is failing than that Taz never changes state,
    # and the two look identical from the outside.
    print(f"\n  Checking the chain [[TAZ_PTR] + 0x1C0] + 0x200:\n")
    try:
        taz = G.mem.read_u32(G.TAZ_PTR)
        print(f"    [0x{G.TAZ_PTR:06X}]        = 0x{taz:08X}"
              f"   {'valid' if G.mem.valid_ptr(taz) else 'NOT A POINTER'}")
        if not G.mem.valid_ptr(taz):
            print("\n    Taz's object does not exist -- are you in a level "
                  "rather than a menu?\n")
            return
        obj = G.mem.read_u32(taz + G.O_STATE_PTR)
        print(f"    [0x{taz + G.O_STATE_PTR:08X}]  = 0x{obj:08X}"
              f"   {'valid' if G.mem.valid_ptr(obj) else 'NOT A POINTER'}")
        if not G.mem.valid_ptr(obj):
            print("\n    The state object is missing. The offset 0x1C0 may be "
                  "wrong for this build.\n")
            return
        final = obj + G.S_STATE
        val = G.mem.read_u8(final)
        print(f"    state byte at 0x{final:08X} = 0x{val:02X}")
        chain = g.taz_state()
        print(f"    taz_state() returns          "
              f"{('0x%02X' % chain) if chain is not None else 'None'}")
        if chain is None:
            print("\n    The chain reads but taz_state() does not -- so the "
                  "fault is in how it dereferences, not in the addresses.")
            print("    Falling back to reading the byte directly.\n")
    except Exception as exc:
        print(f"    the chain raised: {exc}\n")
        return

    print(f"\n  Logging states for {secs:.0f}s.")
    print(f"  Die by falling a few times, then use an in-level warp.\n")
    print(f"  {'time':>6}  {'state':>5}  {'held':>6}  {'level':<24} name")

    last = None
    since = time.time()
    seen = {}
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.02)
        # Read the chain here rather than trusting the helper: if the helper
        # is what is broken, the tool should still be able to see states.
        st = None
        try:
            taz = G.mem.read_u32(G.TAZ_PTR)
            if G.mem.valid_ptr(taz):
                obj = G.mem.read_u32(taz + G.O_STATE_PTR)
                if G.mem.valid_ptr(obj):
                    st = G.mem.read_u8(obj + G.S_STATE)
        except Exception:
            st = None
        if st is None or st == last:
            continue
        now = time.time()
        if last is not None:
            lid = g.level_id()
            where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
            name = STATE_NAMES.get(last, "UNKNOWN")
            seen[last] = seen.get(last, 0) + 1
            print(f"  {now - t0:>6.1f}  0x{last:02X}  {now - since:>6.2f}  "
                  f"{str(where):<24} {name}")
        last, since = st, now

    print(f"\n  states seen:\n")
    for st, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        name = STATE_NAMES.get(st, "UNKNOWN  <-- worth a look")
        print(f"    0x{st:02X}  {n:>4} time(s)  {name}")
    print("\n  A state that appeared only when you fell, and never during a")
    print("  warp, is the one to use.\n")


def candidate(g, addr, secs):
    """Compare a suspected void-out address against the current detector.

    The point is not to see the address move -- it is to see it move at the
    RIGHT times. A flag worth switching to fires on every real death and stays
    put through an in-level warp, which is exactly where inferring from a
    position jump goes wrong.
    """
    print(f"\n  Watching 0x{addr:08X} for {secs:.0f}s, beside the current")
    print(f"  detector. Die a few ways, then use an in-level warp.\n")
    print(f"  {'time':>6}  {'level':<26} {'0x%06X' % addr:>10}  "
          f"{'jump':>7}  what")

    last_val = None
    last_pos = None
    agree = disagree_missed = disagree_false = 0
    t0 = time.time()

    while time.time() - t0 < secs:
        time.sleep(0.05)
        lid = g.level_id()
        if lid is None:
            continue
        name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
        pos = g._pos()
        jump = math.dist(pos, last_pos) if (pos and last_pos) else 0.0
        try:
            val = G.mem.read_u32(addr)
        except Exception:
            continue

        kind = g.death_tick()
        moved = last_val is not None and val != last_val

        if moved or kind:
            note = []
            if moved:
                note.append(f"the address moved {last_val} -> {val}")
            if kind:
                note.append(f"the detector says {kind}")
            if moved and kind:
                agree += 1
                verdict = "AGREE"
            elif moved:
                disagree_missed += 1
                verdict = "the address fired, the detector did NOT"
            else:
                disagree_false += 1
                verdict = "the detector fired, the address did NOT"
            print(f"  {time.time() - t0:>6.1f}  {str(name):<26} "
                  f"{val:>10}  {jump:>7.0f}  {verdict}")
            for n in note:
                print(f"  {'':>6}  {n}")

        last_val = val
        if pos:
            last_pos = pos

    print(f"\n  {agree} agreed, "
          f"{disagree_missed} the address caught alone, "
          f"{disagree_false} the detector alone")
    print("  The address is worth switching to if it caught the deaths and")
    print("  stayed put through the warps -- the detector firing alone on a")
    print("  warp is exactly the fault being chased.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="watch",
                    choices=("watch", "verbose", "candidate", "states",
                             "scanobj", "compare"))
    ap.add_argument("--secs", type=float, default=600.0)
    ap.add_argument("--addr", default="0xA7AF08",
                    help="the suspected void-out address")
    a = ap.parse_args()
    verbose = a.cmd == "verbose"

    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True
    g.refresh_save_file()

    if a.cmd == "candidate":
        candidate(g, int(a.addr, 0), a.secs)
        return
    if a.cmd == "states":
        states(g, a.secs)
        return
    if a.cmd == "scanobj":
        scanobj(g, a.secs)
        return
    if a.cmd == "compare":
        compare(g)
        return

    print(f"\n  Watching for {a.secs:.0f}s. Do the thing you suspect.\n")
    if verbose:
        print("  Verbose: state and position changes are shown even when "
              "nothing is reported.\n")
    print(f"  {'time':>6}  {'level':<26} {'state':<14} {'jump':>8}  what")

    last_state = None
    last_pos = None
    deaths = {"captures": 0, "void_out": 0}
    t0 = time.time()

    while time.time() - t0 < a.secs:
        time.sleep(0.05)
        lid = g.level_id()
        if lid is None:
            continue
        name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
        in_hub = lid in G.HUBS

        pos = g._pos()
        st = g.taz_state()
        jump = 0.0
        if pos and last_pos:
            jump = math.dist(pos, last_pos)

        # Ask the client's own detector, so what is printed is what would
        # actually be sent -- not a re-implementation that could disagree.
        kind = g.death_tick()

        if kind:
            deaths[kind] = deaths.get(kind, 0) + 1
            why = ("the caught state" if kind == "captures" else
                   ("the drown state" if st == 0x2C else
                    f"a position jump of {jump:.0f}"))
            print(f"  {time.time() - t0:>6.1f}  {str(name):<26} "
                  f"{STATE_NAMES.get(st, hex(st) if st is not None else '?'):<14} "
                  f"{jump:>8.0f}  DEATH: {kind} -- {why}")
            if in_hub:
                print(f"  {'':>6}  in a hub, where inferred voids are ignored "
                      f"-- so this was a real state")
        elif verbose and (st != last_state or jump > 200):
            note = ""
            if jump > 1500 and in_hub:
                note = "  (big jump in a hub -- ignored, probably a warp door)"
            elif jump > 1500:
                note = "  (big jump, but attributed to a recent death)"
            print(f"  {time.time() - t0:>6.1f}  {str(name):<26} "
                  f"{STATE_NAMES.get(st, hex(st) if st is not None else '?'):<14} "
                  f"{jump:>8.0f} {note}")

        last_state = st
        if pos:
            last_pos = pos

    print(f"\n  {deaths['captures']} capture(s), {deaths['void_out']} void(s)")
    print("  A void that should not have counted, or a death that was missed,")
    print("  is worth pasting with the lines around it.\n")


if __name__ == "__main__":
    main()
