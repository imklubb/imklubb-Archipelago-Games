"""
taz_catcher_doctor.py -- see how catchers are being identified, and why one
gets the wrong name.

Keepers patrol, so where one dies says nothing about which one it was. Identity
comes from where a keeper is FIRST seen -- its post -- matched against the
recorded table. When two posts are close together, or a keeper is first seen
part-way along its patrol, the match can land on the wrong entry.

Run it next to Launcher.py with the client CLOSED (PINE allows one connection):

    venv\\Scripts\\python.exe taz_catcher_doctor.py

    live      what is loaded right now, and what each one matches
    spread    how close the recorded posts are, per level
    watch     follow keepers as you play, and report every match

`spread` is the one to look at first: a level whose closest pair is under the
matching radius cannot be told apart reliably, and the radius has to come down
or the posts need re-recording.
"""

import argparse
import itertools
import json
import math
import os
import sys

# Imported as worlds.tazwanted, NOT by putting the world's folder on sys.path:
# doing that makes `import Options` find tazwanted/Options.py instead of
# Archipelago's, and the circular import that follows is baffling to read.
try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import _imports
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"Run this from the Archipelago folder, next to Launcher.py.")

# A stale install is easy to miss: a built .apworld in custom_worlds takes
# precedence over the source folder, so the doctor can be reading last week's
# code while the source is current. Saying which file was loaded makes that
# obvious instead of producing an AttributeError halfway through.
_REQUIRED = ("catcher_debug", "start_catchers", "catcher_tick")
_missing = [n for n in _REQUIRED if not hasattr(G.Game, n)]
if _missing:
    sys.exit(
        f"the installed world is out of date -- it has no {_missing[0]}.\n"
        f"  loaded from: {G.__file__}\n"
        f"  Update that copy (and delete any stale tazwanted.apworld in\n"
        f"  custom_worlds, which takes precedence over worlds/tazwanted).")


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def table():
    return _imports.data("taz_catchers.json") or {}


def cmd_spread():
    """How far apart the recorded posts are, against the matching radius."""
    t = table()
    radius = G.CATCHER_MATCH_RADIUS
    print(f"\n  matching radius {radius}\n")
    for lid, rec in sorted(t.items(), key=lambda kv: int(kv[0])):
        cs = rec["catchers"]
        if len(cs) < 2:
            print(f"    {rec['name']:<26} {len(cs)} catcher, nothing to "
                  f"confuse")
            continue
        pairs = [(dist(a["pos"], b["pos"]), a["name"], b["name"])
                 for a, b in itertools.combinations(cs, 2)]
        g, an, bn = min(pairs)
        flag = ""
        if g < radius * 2:
            flag = "   <-- too close to tell apart"
        print(f"    {rec['name']:<26} {len(cs)} catchers, closest {g:>7.0f}"
              f"{flag}")
        if flag:
            print(f"        {an}")
            print(f"        {bn}")
    print("\n  A pair closer than twice the radius can match the wrong entry.")
    print("  Either lower CATCHER_MATCH_RADIUS or re-record those posts.\n")


def cmd_live():
    """What is loaded now, and which recorded post each keeper matches."""
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the client still open?")
    g = G.Game()
    g.connected = True
    lid = g.level_id()
    t = table()
    rec = t.get(str(lid))
    print(f"\n  {G.T.LEVEL_IDS.get(lid, lid)}")
    if not rec:
        print("  no catchers recorded for this level\n")
        return
    try:
        keepers = G.T.TazPS2(G.mem).catchers()
    except Exception as exc:
        sys.exit(f"could not read the keepers: {exc}")

    print(f"  {len(keepers)} loaded, {len(rec['catchers'])} recorded\n")
    for k in keepers:
        pos = k.get("pos")
        if not pos:
            continue
        ranked = sorted(
            ((dist(pos, c["pos"]), i, c["name"])
             for i, c in enumerate(rec["catchers"])))
        best_d, best_i, best_n = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        mark = ""
        if best_d > G.CATCHER_MATCH_RADIUS:
            mark = "   NO MATCH -- outside the radius"
        elif second and second[0] - best_d < G.CATCHER_MATCH_RADIUS / 2:
            mark = "   AMBIGUOUS -- the runner-up is nearly as close"
        print(f"    0x{k['ptr']:08X} at "
              f"({pos[0]:>9.0f},{pos[1]:>8.0f},{pos[2]:>9.0f})"
              f"  defeated={k.get('defeated')}")
        print(f"      -> [{best_i}] {best_n}  ({best_d:.0f} away){mark}")
        if second:
            print(f"         next: {second[2]} ({second[0]:.0f} away)")
    print()


def cmd_states(secs):
    """Log Taz's state as you play, so a safe set can be built from evidence.

    Burp has to wait for a state where interrupting him will not break the
    model. Listing the bad states did not work -- the spin values are shared
    with the cage-escape chain -- so the client waits for idle instead. If
    that feels sluggish, run this, note which value shows while simply
    running, and it can be added.
    """
    import time
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the client still open?")
    g = G.Game()
    g.connected = True
    print(f"\n  {secs:.0f}s. Stand still, walk, run, spin, jump -- and note")
    print("  which value each one shows.\n")
    seen = {}
    prev = None
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.05)
        st = g.taz_state()
        if st is None or st == prev:
            continue
        prev = st
        seen[st] = seen.get(st, 0) + 1
        print(f"  {time.time() - t0:>6.1f}  state 0x{st:02X} ({st})")
    print("\n  how often each appeared:\n")
    for st, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        tag = "   <-- currently treated as safe" \
            if st in G.Game.SAFE_TO_INTERRUPT else ""
        print(f"    0x{st:02X} ({st:>3})  {n:>3}{tag}")
    print()


def cmd_verify(secs, out_path="taz_catcher_verify.json"):
    """Name each catcher as you kill it, and record what it was matched on.

    Identity comes from where a keeper is FIRST seen, on the assumption that
    the first sighting is its post. A keeper first spotted part-way along its
    patrol matches the wrong entry, or nothing at all -- and this shows which,
    with the distances, so the table can be corrected from evidence rather
    than guesswork.

    Kill catchers one at a time. Each kill prints the match and the runners-up.
    """
    import time
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the client still open?")
    g = G.Game()
    g.connected = True
    t = table()
    g.start_catchers(t)

    print(f"\n  Kill catchers one at a time. {secs:.0f}s.\n")
    log = []
    first_seen = {}
    end = time.time() + secs
    while time.time() < end:
        time.sleep(0.05)
        lid = g.level_id()
        rec = t.get(str(lid))
        if not rec:
            continue
        posts = rec["catchers"]

        try:
            keepers = G.T.TazPS2(G.mem).catchers()
        except Exception:
            continue
        for k in keepers:
            if k["ptr"] not in first_seen and k.get("pos"):
                first_seen[k["ptr"]] = tuple(k["pos"])

        for level, index in g.catcher_tick():
            name = (posts[index]["name"] if index < len(posts)
                    else f"index {index}")
            print(f"\n    killed -> [{index}] {name}")
            entry = {"level": level, "index": index, "name": name}

            # Where was it first seen, and how well did that match?
            ptr = None
            for p, pos in first_seen.items():
                d = dist(pos, posts[index]["pos"]) if index < len(posts) else 1e9
                if ptr is None or d < ptr[1]:
                    ptr = (p, d, pos)
            if ptr:
                entry["first_seen"] = [round(v, 1) for v in ptr[2]]
                entry["distance"] = round(ptr[1], 1)
                print(f"      first seen at "
                      f"({ptr[2][0]:.0f}, {ptr[2][1]:.0f}, {ptr[2][2]:.0f})"
                      f"  {ptr[1]:.0f} from that post")
                ranked = sorted((dist(ptr[2], c["pos"]), i, c["name"])
                                for i, c in enumerate(posts))
                for d, i, n in ranked[:3]:
                    mark = "  <-- chosen" if i == index else ""
                    print(f"        [{i}] {n:<34} {d:>7.0f}{mark}")
                if ranked[0][1] != index:
                    print(f"      MISMATCH: the nearest post is "
                          f"[{ranked[0][1]}] {ranked[0][2]}")
                    entry["nearest"] = ranked[0][2]
            log.append(entry)

    if log:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
        print(f"\n  {len(log)} kill(s) -> {out_path}")
        print("  Any line marked MISMATCH is a post recorded in the wrong")
        print("  place, or a keeper first seen away from its post. Fix it")
        print("  with:  taz_catcher_doctor.py rename <level> <index> \"name\"")
    print()


def cmd_rename(level, index, name, path=None):
    """Correct a catcher's name in the table."""
    if path is None:
        path = os.path.join("worlds", "tazwanted", "data",
                            "taz_catchers.json")
    if not os.path.exists(path):
        sys.exit(f"cannot find {path}")
    with open(path, encoding="utf-8") as f:
        t = json.load(f)
    rec = t.get(str(level))
    if not rec or index >= len(rec["catchers"]):
        sys.exit(f"no catcher [{index}] in level {level}")
    old = rec["catchers"][index]["name"]
    rec["catchers"][index]["name"] = name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(t, f, indent=2)
    print(f"\n  {rec['name']} [{index}]")
    print(f"    {old!r} -> {name!r}")
    print(f"  Written to {path}. Rebuild the apworld to pick it up.\n")


def cmd_record(secs):
    """Log everything the kill detection sees, so a real kill can be studied.

    Run it, kill exactly one catcher, and stop. The output shows what changed
    at that moment across every signal at once -- which is the only way to tell
    which of them is actually reliable.
    """
    import time
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the client still open?")
    g = G.Game()
    g.connected = True
    print(f"\n  recording for {secs:.0f}s. Kill ONE catcher, then wait.\n")
    print(f"  {'time':>6}  {'total':>5}  {'loaded':>6}  {'beaten':>6}  "
          f"{'costume':>7}  {'state':>5}")
    prev = None
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.1)
        d = g.catcher_debug()
        key = (d.get("enemy_total"), d.get("loaded"), d.get("defeated"),
               d.get("costume"), d.get("state"))
        if key == prev:
            continue
        prev = key
        print(f"  {time.time() - t0:>6.1f}  "
              f"{str(d.get('enemy_total')):>5}  {str(d.get('loaded')):>6}  "
              f"{str(d.get('defeated')):>6}  {str(d.get('costume')):>7}  "
              f"{str(d.get('state')):>5}")
    print("\n  Paste this. The line where the kill happened tells us which")
    print("  column actually moved, and that is the signal to use.\n")


def cmd_watch(secs):
    """Follow keepers while you play, reporting each identification."""
    import time
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the client still open?")
    g = G.Game()
    g.connected = True
    g.start_catchers(table())
    print(f"\n  watching for {secs:.0f}s. Play normally; kills are reported "
          f"as they are matched.\n")
    seen = {}
    end = time.time() + secs
    while time.time() < end:
        time.sleep(0.05)
        lid = g.level_id()
        for level, index in g.catcher_tick():
            rec = table().get(str(level), {})
            names = [c["name"] for c in rec.get("catchers", [])]
            name = names[index] if index < len(names) else f"index {index}"
            print(f"    killed -> [{index}] {name}")
        try:
            keepers = G.T.TazPS2(G.mem).catchers()
        except Exception:
            continue
        for k in keepers:
            if k["ptr"] in seen or not k.get("pos"):
                continue
            seen[k["ptr"]] = k["pos"]
            rec = table().get(str(lid), {})
            if not rec:
                continue
            ranked = sorted((dist(k["pos"], c["pos"]), i, c["name"])
                            for i, c in enumerate(rec["catchers"]))
            d, i, n = ranked[0]
            print(f"    first seen 0x{k['ptr']:08X} -> [{i}] {n} "
                  f"({d:.0f} away)")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="spread",
                    choices=("spread", "live", "watch", "record", "verify",
                             "rename", "states"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--secs", type=float, default=120.0)
    a = ap.parse_args()
    if a.cmd == "spread":
        cmd_spread()
    elif a.cmd == "live":
        cmd_live()
    elif a.cmd == "record":
        cmd_record(a.secs)
    elif a.cmd == "verify":
        cmd_verify(a.secs)
    elif a.cmd == "states":
        cmd_states(a.secs)
    elif a.cmd == "rename":
        if len(a.args) < 3:
            sys.exit('usage: taz_catcher_doctor.py rename <level> <index> '
                     '"New Name"')
        cmd_rename(int(a.args[0]), int(a.args[1]), " ".join(a.args[2:]))
    else:
        cmd_watch(a.secs)


if __name__ == "__main__":
    main()
