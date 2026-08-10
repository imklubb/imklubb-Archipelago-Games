"""
taz_exit_recorder.py -- record where each level actually ends.

A level is finished by destroying all seven wanted posters AND walking to the
spot that ends it. Using the posters alone would award the check the moment the
seventh breaks, wherever the player happens to be standing -- which is not how
the game behaves.

So the exit positions have to be recorded, once, from the game itself.

Run it next to Launcher.py with the AP client CLOSED (PINE allows one
connection per slot):

    venv\\Scripts\\python.exe taz_exit_recorder.py

Then, for each level: load it, walk to the spot that ends the level, and press
Enter. It writes taz_exits.json, which goes in worlds/tazwanted/data/.

Tazland is the exception worth remembering: its exit is the Disco Volcano
entrance that is NOT beside the Hindenbird door, so stand at that one.

Commands, typed at the prompt:

    (Enter)   refresh -- where Taz is now, and which level
    rec       record this spot as the current level's exit
    watch     follow Taz live, showing whether he is inside the radius.
              Ctrl-C to stop watching and return to the prompt
    r 900     set the radius for the next recording (default 700)
    show      what has been recorded so far
    drop      remove the current level's entry
    quit      write the file and stop

Enter only refreshes. Recording is deliberately a word, because the prompt
blocks while it waits: pressing Enter to see where you are would otherwise
save the position from before you moved.
"""

import time

import json
import math
import os
import sys

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run it by path instead:")
    print("      venv\\Scripts\\python.exe taz_exit_recorder.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

OUT = "taz_exits.json"
DEFAULT_RADIUS = 700.0


def load():
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save(data):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def watch(g, data):
    """Follow Taz live, so a radius can be judged rather than guessed.

    Walk in and out of the exit and see where the boundary falls. A radius that
    is too small means walking onto the exact spot; too large means the level
    completes as you pass nearby.
    """
    print("\n  Watching. Walk around the exit. Ctrl-C to stop.\n")
    last = None
    try:
        while True:
            time.sleep(0.15)
            lid = g.level_id()
            rec = data.get(str(lid))
            pos = g._pos()
            if not pos:
                continue
            if not rec:
                line = (f"  {L.LEVEL_NAME.get(lid, lid)}: nothing recorded "
                        f"here yet")
            else:
                d = math.dist(pos, rec["pos"])
                inside = d <= rec["radius"]
                bar = "#" * min(40, int(d / rec["radius"] * 20))
                line = (f"  {d:>8.0f} / {rec['radius']:.0f}  "
                        f"{'INSIDE ' if inside else 'outside'}  {bar}")
            if line != last:
                last = line
                print(line)
    except KeyboardInterrupt:
        print("\n  ...stopped watching.\n")


def main():
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True

    data = load()
    radius = DEFAULT_RADIUS
    print(f"\n  Recording level exits. {len(data)} already saved.")
    print(f"  Walk to the spot that ends the level, then press Enter.\n")

    while True:
        g.refresh_save_file()
        lid = g.level_id()
        name = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
        pos = g._pos()
        here = ""
        if pos:
            here = f"({pos[0]:>9.1f}, {pos[1]:>8.1f}, {pos[2]:>9.1f})"
            rec = data.get(str(lid))
            if rec:
                d = math.dist(pos, rec["pos"])
                here += f"   {d:>7.0f} from the saved exit"

        try:
            cmd = input(f"  [{name}] {here}\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd in ("quit", "q", "exit"):
            break
        if cmd == "watch":
            watch(g, data)
            continue
        if cmd == "show":
            print()
            for k, v in sorted(data.items(), key=lambda kv: int(kv[0])):
                p = v["pos"]
                print(f"    {v['name']:<26} r={v['radius']:<6.0f} "
                      f"({p[0]:>9.1f}, {p[1]:>8.1f}, {p[2]:>9.1f})")
            print(f"\n    {len(data)} of 10 recorded\n")
            continue
        if cmd == "drop":
            if data.pop(str(lid), None):
                save(data)
                print(f"    removed {name}\n")
            else:
                print(f"    nothing saved for {name}\n")
            continue
        if cmd.startswith("r "):
            try:
                radius = float(cmd.split()[1])
                print(f"    radius for the next recording: {radius:.0f}\n")
            except (IndexError, ValueError):
                print("    usage: r 900\n")
            continue
        if cmd and cmd != "rec":
            print("    rec to record. Also: watch | r <radius> | show | "
                  "drop | quit\n")
            continue
        if cmd != "rec":
            # Bare Enter just refreshes the line above.
            continue

        if lid not in L.LEVEL_ORDER:
            print(f"    {name} is not a level with an exit -- load one first\n")
            continue
        if not pos:
            print("    could not read Taz's position\n")
            continue
        data[str(lid)] = {"name": L.LEVEL_NAME[lid],
                          "pos": [round(v, 1) for v in pos],
                          "radius": radius}
        save(data)
        print(f"    recorded {L.LEVEL_NAME[lid]} at "
              f"({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}) r={radius:.0f}")
        missing = [n for l, n in L.LEVELS if str(l) not in data]
        print(f"    {len(data)} of 10 done"
              + (f", still to do: {', '.join(missing)}" if missing else "")
              + "\n")

    save(data)
    print(f"\n  Wrote {OUT} with {len(data)} exit(s).")
    print(f"  Copy it into worlds/tazwanted/data/ and rebuild.\n")


if __name__ == "__main__":
    main()
