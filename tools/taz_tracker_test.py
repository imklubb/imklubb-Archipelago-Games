#!/usr/bin/env python3
"""Does the PopTracker pack agree with the world about what is in logic?

    py -3.13 taz_tracker_test.py --pack "..\\Taz Wanted\\Taz-Wanted-Poptracker\\Taz-Wanted-Poptracker"

Needs lua5.4 on PATH. The pack's rules are Lua, so they are RUN rather than
re-implemented here -- a Python copy of them would only prove the copy matches
itself, which is how the two drifted apart in the first place.

For each scenario this works out, for every location any seed could contain:

    Python   region_of -> entrance_rules -> location_rules,   the world
    Lua      the access_rules in locations.json,              the tracker

and prints every disagreement. The four bugs the generator fixes all show up
here as mismatches if the fix is removed, which is the point.
"""

import argparse
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")


def load_world():
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("_imports", "logic"):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.logic"]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def code_for(D, item):
    """Python item name -> the tracker code, mirroring autotracking's codeFor."""
    if item == "Wanted Poster":
        return "poster"
    if item == "Hindenbird Ticket":
        return "ticket"
    m = re.match(r"^(.*) Bonus Game Unlock$", item)
    if m:
        return "bonus_" + slug(m.group(1))
    m = re.match(r"^(.*) Unlock$", item)
    if m:
        base = slug(m.group(1))
        bosses = {slug(D.BOSS_UNLOCK[lid].replace(" Unlock", ""))
                  for lid, _ in D.BOSSES}
        return ("boss_" if base in bosses else "unlock_") + base
    return "costume_" + slug(item)


# ------------------------------------------------------------ the pack's side


def chains(doc):
    """location name -> the access_rules that gate it, group then section."""
    out = {}
    for top in doc:
        for group in top.get("children", []):
            grules = [list(r) for r in (group.get("access_rules") or [])]
            for s in group.get("sections", []):
                nm = s.get("name")
                srules = [list(r) for r in (s.get("access_rules") or [])]
                node = [x for x in (grules, srules) if x]
                # A location shows in two places with the same rules; if they
                # ever differ that is itself a bug, so keep the first and check.
                if nm in out and out[nm] != node:
                    out[nm] = ("CONFLICT", out[nm], node)
                else:
                    out.setdefault(nm, node)
    return out


def lua_literal(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if v is None:
        return "nil"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, dict):
        return "{" + ",".join(f"[{lua_literal(k)}]={lua_literal(x)}"
                              for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "{" + ",".join(lua_literal(x) for x in v) + "}"
    raise TypeError(type(v))


LUA_DRIVER = r'''
-- A stand-in for PopTracker: only the two things the rules touch.
local OWNED = %(owned)s
local COUNTS = %(counts)s
local objs = {}
Tracker = {}
function Tracker:FindObjectForCode(code)
  if objs[code] then return objs[code] end
  local o = {Active = OWNED[code] and true or false,
             AcquiredCount = COUNTS[code] or 0}
  objs[code] = o
  return o
end
SLOT_DATA = %(slot)s
SAND_STEP = %(step)s
SAND_START = %(start)s
DEST_STEP = %(dstep)s
DEST_START = 0
DEST_GOAL = %(dgoal)s
dofile(%(logic)s)

local function callRule(r)
  local name, rest = r:match("^%%$([^|]+)|?(.*)$")
  local args = {}
  for a in rest:gmatch("[^|]+") do args[#args + 1] = a end
  local f = _G[name]
  if not f then error("no such rule function: " .. tostring(name)) end
  local v = f(table.unpack(args))
  return (v == 1 or v == true)
end

-- PopTracker semantics: access_rules is OR of ANDs.
local function anyOf(lists)
  for _, andList in ipairs(lists) do
    local ok = true
    for _, r in ipairs(andList) do
      local v
      if r:sub(1, 1) == "$" then v = callRule(r)
      else v = Tracker:FindObjectForCode(r).Active end
      if not v then ok = false break end
    end
    if ok then return true end
  end
  return #lists == 0
end

local CHAINS = %(chains)s
local out = {}
for i, chain in ipairs(CHAINS) do
  local ok = true
  for _, lists in ipairs(chain) do
    if not anyOf(lists) then ok = false break end
  end
  out[#out + 1] = ok and "1" or "0"
end
print(table.concat(out, ""))
'''


def run_lua(pack, owned, counts, slot, chain_list):
    script = LUA_DRIVER % {
        "owned": lua_literal({c: True for c in owned}),
        "counts": lua_literal(counts),
        "slot": lua_literal(slot),
        "step": slot.get("sandwich_checks", 100),
        "start": slot.get("starting_sandwiches", 0),
        "dstep": slot.get("destruction_checks", 50),
        "dgoal": {"standard": 50, "advanced": 75,
                  "expert": 100}[slot.get("difficulty", "standard")],
        "logic": lua_literal(os.path.join(pack, "scripts", "ap_logic.lua")),
        "chains": lua_literal(chain_list),
    }
    p = subprocess.run(["lua5.4", "-"], input=script, capture_output=True,
                       text=True)
    if p.returncode != 0:
        raise SystemExit("lua failed:\n" + p.stderr[:2000])
    return p.stdout.strip()


# ---------------------------------------------------------- the world's side


def py_reachable(D, locs, mode, options, state, catchers):
    er = D.entrance_rules(mode, options)
    lr = D.location_rules(mode, options, options["difficulty"], catchers)
    out = {}
    for loc in locs:
        region = D.region_of(loc, mode, options["difficulty"],
                             options["starting_sandwiches"])
        rule = er.get(region)
        ok = rule(state) if rule else True
        extra = lr.get(loc["name"])
        if ok and extra:
            ok = bool(extra(state))
        out[loc["name"]] = bool(ok)
    return out


def kits(D):
    """Item sets to test with.

    The PARTIAL ones are the point. An all-or-nothing sweep cannot see a rule
    that wrongly requires two items, because both are present or both absent
    either way -- and "bonus games also demanded the level unlock" is exactly
    that shape. It is the bug Caleb actually hit, and the first version of this
    test passed straight over it.
    """
    return {
        "nothing": [],
        "everything": (D.LEVEL_UNLOCKS + D.BOSS_UNLOCKS + D.COSTUMES
                       + D.BONUS_UNLOCKS),
        "bonus only": D.BONUS_UNLOCKS,          # the reported bug
        "levels only": D.LEVEL_UNLOCKS,
        "costumes only": D.COSTUMES,
        "levels + costumes": D.LEVEL_UNLOCKS + D.COSTUMES,
        "all but levels": D.BOSS_UNLOCKS + D.COSTUMES + D.BONUS_UNLOCKS,
        "half the levels": D.LEVEL_UNLOCKS[::2] + D.BONUS_UNLOCKS,
    }


# Goal Conditions, as the option's own values. The Hindenbird is gated on the
# goal in Open, and the unlock item exists only when the goal includes it -- so
# a sweep that never varies this cannot see the last fight at all.
#
# Indices, not the old three booleans: those keys no longer exist, and passing
# them to normalise now lands them in the "keep anything else the caller sent"
# branch where the real expansion promptly overwrites them. The test would
# still have run, silently, on nothing but the default goal.
GOALS = [0, 1, 2, 6]        # posters, bosses, unlock, all three


def scenarios(D):
    """Enough combinations to exercise both modes and both Tazland sides."""
    for mode, diff, start, posters, goal, tickets in itertools.product(
            ("open", "linear"), ("standard", "expert"), (0, 25), (0, 70),
            GOALS, (0, 4)):
        for kit in kits(D):
            yield mode, diff, start, posters, goal, tickets, kit


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    D = load_world()
    doc = json.load(open(os.path.join(args.pack, "locations",
                                      "locations.json"), encoding="utf-8"))
    try:
        # STRING keys -- see the note in taz_tracker_gen.py.
        catchers = json.load(open(
            os.path.join(WORLD, "data", "taz_catchers.json"),
            encoding="utf-8"))
    except Exception:
        catchers = {}

    ch = chains(doc)
    bad = [k for k, v in ch.items() if isinstance(v, tuple)]
    if bad:
        print(f"    {len(bad)} location(s) gated differently on the two maps:")
        for b in bad[:5]:
            print("      " + b)

    every = D.all_locations(sandwich_interval=1, destruction_interval=1,
                            difficulty="expert", catchers=catchers,
                            with_tickets=True)
    # The Hindenbird's own check is the victory EVENT: beating Tweety is the
    # goal, so it has no address and all_locations leaves it out. The tracker
    # still draws it, and it is the only thing the Hindenbird rule gates --
    # so without this line that rule is never compared against anything, and a
    # regression in it passes silently.
    every.append({"name": D.BOSSES[-1][1], "type": "boss",
                  "level": D.HINDENBIRD_LEVEL})
    # Only what the pack draws: the eighty sandwich thresholds without a pin
    # of their own live in a counter, which has no logic of its own to test.
    names = [l["name"] for l in every if l["name"] in ch
             and not isinstance(ch[l["name"]], tuple)]
    by_name = {l["name"]: l for l in every}
    chain_list = [ch[n] for n in names]
    print(f"    {len(names)} locations with rules to compare")

    total_bad, runs = 0, 0
    for mode, diff, start, posters, goal, tickets, kit in scenarios(D):
        o = D.normalise({"game_mode": mode, "difficulty": diff,
                         "starting_sandwiches": start, "sandwich_checks": 1,
                         "destruction_checks": 1, "goal_conditions": goal})
        o["mode"] = mode

        state = {"Wanted Poster": posters, "Hindenbird Ticket": tickets}
        owned, counts = set(), {"poster": posters, "ticket": tickets}
        pool = set(D.progression_items(mode, o)) | set(D.useful_items(mode, o))
        for item in kits(D)[kit]:
            # Only hand out items the seed actually contains. The Hindenbird
            # unlock is in the pool only when it is part of the goal, and
            # pretending to hold one that was never generated would test a
            # state no player can be in.
            if item not in pool:
                continue
            state[item] = 1
            owned.add(code_for(D, item))
        # Linear gates on beaten bosses, which the world models as events.
        # Reaching a boss is what grants one, so give the events the world
        # itself would have swept in.
        for lid, ev in sorted(D.BEATEN_EVENT.items()):
            short = D.BOSS_UNLOCK[lid].replace(" Unlock", "")
            state[ev] = 1 if _boss_reachable(D, mode, o, state, lid) else 0

        want = py_reachable(D, [by_name[n] for n in names], mode, o, state,
                            catchers)
        got = run_lua(args.pack, owned, counts, o, chain_list)
        runs += 1

        diffs = [(n, want[n], got[i] == "1")
                 for i, n in enumerate(names) if want[n] != (got[i] == "1")]
        if diffs:
            total_bad += len(diffs)
            print(f"\n    {mode}/{diff}/start={start}/posters={posters}/"
                  f"goal={''.join(c for c, g in zip('pbu', D.GOAL_COMBOS[goal]) if g)}"
                  f"/tickets={tickets}/{kit}: {len(diffs)} mismatch(es)")
            shown = diffs if args.verbose else diffs[:6]
            for n, w, g in shown:
                print(f"      {n}")
                print(f"        world says {'in' if w else 'OUT of'} logic, "
                      f"tracker says {'in' if g else 'OUT of'} logic")
            if len(diffs) > len(shown):
                print(f"      ... and {len(diffs) - len(shown)} more")

    print(f"\n    {runs} scenarios, {total_bad} mismatch(es)")
    print("    ALL AGREE" if not total_bad else "    *** DISAGREEMENT ***")
    return 1 if total_bad else 0


def _boss_reachable(D, mode, options, state, lid):
    """Whether the world would have swept this boss's Beaten event in."""
    er = D.entrance_rules(mode, options)
    short = D.BOSS_UNLOCK[lid].replace(" Unlock", "")
    rule = er.get(short)
    if not rule:
        return True
    # Beaten events chain, so resolve them in LINEAR_ORDER and feed each one
    # back in before judging the next.
    return bool(rule(state))


if __name__ == "__main__":
    raise SystemExit(main())
