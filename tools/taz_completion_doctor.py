"""
taz_completion_doctor.py -- find out what actually marks a level complete.

Tazland raises a question the other levels do not. Every level's completion
flag is written by the game at some point, but for most of them that point is
obvious: you finish the level and leave. Tazland has no exit of its own -- it
runs into Disco Volcano -- so if the flag is only set on entering the boss, and
the boss is locked, the completion check can never fire.

This watches the flag alongside everything that might drive it, so the answer
comes from the game rather than from a guess.

Run it next to Launcher.py with the AP client CLOSED (PINE allows one
connection per slot):

    venv\\Scripts\\python.exe taz_completion_doctor.py

    watch          follow the current level's completion state
    all            every level's flag at once, once
    posters        just the poster count, for the level you are in

The useful run is `watch` in Tazland: destroy the seventh poster and see
whether the flag moves. If it does, completion is poster-driven and nothing
needs changing. If it does not, the check has to be derived instead.
"""

import argparse
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run it by path instead:")
    print("      venv\\Scripts\\python.exe taz_completion_doctor.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")


def connect():
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True
    g.refresh_save_file()
    return g


def poster_bits(g, lid):
    """Which of the seven posters are destroyed, individually."""
    base = L.level_block(lid, g.save_file)
    out = []
    for i in range(L.POSTERS_PER_LEVEL):
        try:
            out.append(bool(G.mem.read_u32(base + L.L_POSTER + i * 4)))
        except Exception:
            out.append(False)
    return out


def snapshot(g, lid):
    base = L.level_block(lid, g.save_file)
    rd = lambda off: g._u32(base + off)
    bits = poster_bits(g, lid)
    return {
        "complete": rd(L.L_COMPLETE),
        "posters": sum(bits),
        "bits": "".join("1" if b else "0" for b in bits),
        "posters_done": rd(L.L_POSTERS_DONE),
        "sandwiches": rd(L.L_SANDWICHES),
        "destruction": rd(L.L_DESTRUCTION),
        "statue": rd(L.L_GOLDEN_SAM),
        "bonus": rd(L.L_BONUS_GAME),
    }


def cmd_watch(secs):
    g = connect()
    lid = g.level_id()
    name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
    print(f"\n  {name}, save file {g.save_file + 1}")
    print(f"  Watching for {secs:.0f}s. Destroy the last poster and see "
          f"whether `complete` moves.\n")
    print(f"  {'time':>6}  {'complete':>8}  {'posters':>7}  {'bits':>7}  "
          f"{'done':>4}  {'sand':>4}  {'dest':>4}  {'sam':>3}  {'bonus':>5}")

    prev = None
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.1)
        now = g.level_id()
        if now != lid:
            lid = now
            name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
            print(f"\n  -- now in {name} --\n")
            prev = None
            continue
        if lid not in L.LEVEL_ORDER:
            continue
        s = snapshot(g, lid)
        key = tuple(s.values())
        if key == prev:
            continue
        # Completion is what this is about, so say when it moves.
        if prev is not None and s["complete"] != prev[0]:
            print(f"  {'':>6}  *** complete {prev[0]} -> {s['complete']} "
                  f"with {s['posters']}/7 posters ***")
        prev = key
        print(f"  {time.time() - t0:>6.1f}  {s['complete']:>8}  "
              f"{s['posters']:>7}  {s['bits']:>7}  {s['posters_done']:>4}  "
              f"{s['sandwiches']:>4}  {s['destruction']:>4}  "
              f"{s['statue']:>3}  {s['bonus']:>5}")
    print()


def cmd_all():
    g = connect()
    print(f"\n  save file {g.save_file + 1}\n")
    print(f"  {'level':<26} {'complete':>8} {'posters':>7} {'done':>4} "
          f"{'sand':>4} {'dest':>4} {'sam':>3} {'bonus':>5}")
    for lid, name in L.LEVELS:
        s = snapshot(g, lid)
        print(f"  {name:<26} {s['complete']:>8} {s['posters']:>7} "
              f"{s['posters_done']:>4} {s['sandwiches']:>4} "
              f"{s['destruction']:>4} {s['statue']:>3} {s['bonus']:>5}")
    print("\n  A level you have finished should read complete=1. If Tazland")
    print("  reads 0 after a full clear, its flag is set somewhere you cannot")
    print("  reach, and the check has to come from the posters instead.\n")


def cmd_posters(secs):
    g = connect()
    lid = g.level_id()
    name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
    print(f"\n  {name}: destroy posters and watch the count.\n")
    names = L.POSTER_NAMES.get(name, [])
    prev = None
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.1)
        if g.level_id() != lid:
            continue
        bits = poster_bits(g, lid)
        if bits == prev:
            continue
        for i, (was, now) in enumerate(zip(prev or [False] * 7, bits)):
            if now and not was:
                label = names[i] if i < len(names) else f"poster {i + 1}"
                print(f"  {time.time() - t0:>6.1f}  destroyed [{i}] {label}")
        prev = bits
        if all(bits):
            print(f"\n  all seven destroyed -- complete reads "
                  f"{snapshot(g, lid)['complete']}\n")
    print()


def cmd_tazland(secs, granted=False):
    """Test the Tazland rule, and say why when it does not fire.

    Two cases, and they are meant to behave differently:

      Disco Volcano NOT granted -- the door blocks. Touching it with all seven
        posters destroyed awards the completion, because that is the same
        accomplishment the game measures when you walk in.

      Disco Volcano granted -- the gate is skipped entirely, the player walks
        in, and the game sets the flag itself. Nothing special is needed.

    Pass --granted to try the second case. Without it the zone is treated as
    locked, which is what a fresh seed looks like.
    """
    g = connect()
    g.load_gates()
    dv = next((z for z in g.gates if z["name"] == "disco-volcano"), None)
    if dv is None:
        sys.exit("no disco-volcano zone in taz_gates.json")

    print(f"\n  Disco Volcano zone: radius {dv['radius']:.0f}, "
          f"{len(dv['points'])} point(s)")
    print(f"  treating it as {'GRANTED (no block)' if granted else 'locked'}")
    print(f"\n  Walk into the Disco Volcano door.\n")
    print(f"  {'time':>6}  {'nearest door':>12}  {'posters':>7}  "
          f"{'blocked':>7}  {'complete':>8}")

    import math
    prev = None
    t0 = time.time()
    said = False
    while time.time() - t0 < secs:
        time.sleep(0.1)
        if g.level_id() != 18:
            continue
        pos = g._pos()
        if not pos:
            continue
        d = min(math.sqrt(sum((a - b) ** 2 for a, b in zip(pos, p)))
                for p in dv["points"])

        g.enforce_gates(set(), {19} if granted else set(), None)
        touched = g.touched_disco_volcano
        done = g.posters_done(18)
        flag = g._u32(L.level_block(18, g.save_file) + L.L_COMPLETE)

        row = (round(d / 50), done, touched, flag)
        if row != prev:
            prev = row
            print(f"  {time.time() - t0:>6.1f}  {d:>12.0f}  "
                  f"{'7/7' if done else 'some':>7}  "
                  f"{'yes' if touched else 'no':>7}  {flag:>8}")

        if touched and done and not said:
            said = True
            print(f"\n  *** Tazland - Level Complete would be sent ***\n")
        if flag and not said:
            said = True
            print(f"\n  *** the game set the flag itself -- nothing special "
                  f"needed ***\n")

    if not said:
        print(f"\n  Nothing fired. The reasons, in order:")
        print(f"    - never within {dv['radius']:.0f} of a door point")
        print(f"      (the nearest reading above says how close you got)")
        print(f"    - all seven posters not destroyed")
        print(f"    - Disco Volcano already granted, so the gate is skipped")
        print(f"      and the game sets the flag on entry instead\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="watch",
                    choices=("watch", "all", "posters", "tazland"))
    ap.add_argument("--secs", type=float, default=300.0)
    ap.add_argument("--granted", action="store_true",
                    help="treat Disco Volcano as unlocked")
    a = ap.parse_args()
    if a.cmd == "all":
        cmd_all()
    elif a.cmd == "posters":
        cmd_posters(a.secs)
    elif a.cmd == "tazland":
        cmd_tazland(a.secs, a.granted)
    else:
        cmd_watch(a.secs)


if __name__ == "__main__":
    main()
