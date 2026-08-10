#!/usr/bin/env python3
"""Record what subtitles a level opens with, and whether a stolen pair
survives crossing into another level.

Three questions this answers by recording rather than reasoning:

  1. Does every level open with a subtitle, and how long after the load?
     If it does, the client can steal on entry and have a working pair
     within seconds of any level -- no waiting for the player to wander
     into a hint trigger.
  2. Does a stolen pair survive a level change? Almost certainly not: a
     level load rebuilds that part of the heap. This says so with a
     timestamp and the exact reason the header stopped validating.
  3. Which slot and which flags the game's own messages use, so ours can
     be put on the other slot and stop competing.

    py -3.13 taz_levelwatch.py                 record only, writes nothing
    py -3.13 taz_levelwatch.py --steal-first   also steal the first one
                                               of each level, invisibly
    py -3.13 taz_levelwatch.py --minutes 30 --out levelwatch.json

Play normally: enter levels, leave them, re-enter. Ctrl-C when done.
Stealing is invisible -- the message still runs its full duration and the
game still destroys its own page, so nothing on screen changes.
"""

import argparse
import json
import sys
import time

import taz_steal as T


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--steal-first", action="store_true",
                    help="steal the first message of each level")
    ap.add_argument("--out", default="levelwatch.json")
    args = ap.parse_args()

    p = T.Pine().connect()
    print(f"    watching for {args.minutes} minutes. Ctrl-C to stop early.")
    if args.steal_first:
        for k in ("A", "B"):
            if not T.stub_ok(p, k):
                print(f"    NOTE: no slot {k} stub -- steals will jam the slot")
                break
        print("    will steal the first message of each level")
    print()

    log = []
    level = None
    t_level = time.time()
    seen = set()
    pair = T.load().get("stolen")
    pair_ok = None
    stole_this_level = False
    end = time.time() + args.minutes * 60

    def record(**kw):
        kw["t"] = round(time.time() - t_level, 2)
        kw["level"] = level
        log.append(kw)
        with open(args.out, "w") as fh:
            json.dump(log, fh, indent=2)

    try:
        while time.time() < end:
            lvl = p.r32(T.LEVEL_ID)
            if lvl != level:
                if level is not None:
                    print()
                level, t_level, seen = lvl, time.time(), set()
                stole_this_level = False
                print(f"    == {T.level_name(lvl)} (id {lvl}) ==")
                record(event="level")

            # does the stolen pair still validate?
            if pair:
                n_ok, n_why = T.validate(p, pair["node"], pair.get("node_size"))
                o_ok, o_why = T.validate(p, pair["obj"], pair.get("obj_size"))
                now_ok = n_ok and o_ok
                if pair_ok is None:
                    pair_ok = now_ok
                elif now_ok != pair_ok:
                    pair_ok = now_ok
                    why = "" if now_ok else (n_why if not n_ok else o_why)
                    print(f"      [t+{time.time() - t_level:5.1f}s] stolen pair "
                          f"{'valid again' if now_ok else 'went stale: ' + why}")
                    record(event="pair", valid=now_ok, why=why)

            for node, obj, page in T.find_messages(p):
                if node in seen:
                    continue
                seen.add(node)
                idx = p.r32(obj + T.O_INDEX)
                flags = p.r32(obj + T.O_FLAGS)
                dur = p.f32(obj + T.O_DURATION)
                txt = T.entry_text(p, idx)
                slot = "A" if flags & 2 else "B"
                dt = time.time() - t_level
                print(f"      [t+{dt:5.1f}s] id {idx:5d}  slot {slot}  "
                      f"{dur:.1f}s  {txt!r}")
                record(event="message", id=idx, slot=slot, flags=flags,
                       duration=round(dur, 2), text=txt,
                       node=node, obj=obj, page=page)

                if args.steal_first and not stole_this_level:
                    stole_this_level = True
                    rec = T.steal_from(p, node, obj, page,
                                       log=lambda s: print("       ", s.strip()))
                    if rec:
                        pair, pair_ok = rec, True
                        print(f"        stolen: node 0x{rec['node']:08X}  "
                              f"object 0x{rec['obj']:08X}  "
                              f"slot {'released itself' if rec['freed_itself'] else 'forced'}")
                        record(event="steal", ok=True, node=rec["node"],
                               obj=rec["obj"])
                    else:
                        print("        steal failed")
                        record(event="steal", ok=False)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\n    stopped")

    print()
    levels = {}
    for e in log:
        if e["event"] == "message":
            levels.setdefault(e["level"], []).append(e)
    print("    first subtitle per level entry:")
    for lvl, msgs in sorted(levels.items(), key=lambda kv: (kv[0] is None, kv[0])):
        first = min(msgs, key=lambda m: m["t"])
        print(f"      {T.level_name(lvl):24s} t+{first['t']:6.2f}s  "
              f"id {first['id']:5d}  slot {first['slot']}  {first['text']!r}")
    entered = {e["level"] for e in log if e["event"] == "level"}
    silent = entered - set(levels)
    if silent:
        print()
        print("    entered with NO subtitle seen: "
              + ", ".join(T.level_name(x) for x in sorted(silent)))
        print("    (those levels cannot be relied on for a steal on entry)")
    slots = {}
    for e in log:
        if e["event"] == "message":
            slots[e["slot"]] = slots.get(e["slot"], 0) + 1
    if slots:
        print()
        print("    the game's own messages by slot: "
              + ", ".join(f"{k}={v}" for k, v in sorted(slots.items())))
        quiet = [k for k in ("A", "B") if k not in slots]
        if quiet:
            print(f"    slot {quiet[0]} is unused by the game -- put AP "
                  f"messages there (--flags {'2' if quiet[0] == 'A' else '0'})")
    print(f"\n    {len(log)} events written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
