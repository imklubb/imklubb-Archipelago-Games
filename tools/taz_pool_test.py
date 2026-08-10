#!/usr/bin/env python3
"""Does the item pool contain anything a seed cannot use?

    py -3.13 taz_pool_test.py

No emulator, no generation -- this builds the pool the way the world does and
looks at what came out.

WHAT IS BEING TESTED
--------------------
An item that gates nothing, counts toward nothing, and does nothing when it
arrives is worse than filler: it takes a location slot in this world and in
everybody else's, and it tells the fill algorithm and every hint that it is
worth going for.

Wanted Posters were exactly that. In Open with posters out of the Goal
Conditions the requirement is zero, but the pool still added `poster_pool` of
them as "useful" -- seventy inert items in a hundred and twenty seven item
seed, more than half of it. The one rule that reads them (_hindenbird_rule)
sits behind posters_in_goal, and the gate rules that read them are inside the
LINEAR_ORDER loop, so in that seed there is no reader at all.

The distinction that matters, and the reason this is not just "clamp the pool
to the goal":

    pool ABOVE a goal    deliberate. 70 for a goal of 50 means finding any 50
                         of them, which fills more easily and keeps every one
                         worth picking up. Must survive.
    requirement ZERO     nothing can ever want one. Must be empty.

Both are checked below, for every goal combination and both modes.
"""

import importlib.util
import os
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

WANTED_POSTER = "Wanted Poster"


def load_logic():
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


RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def seed(D, **yaml):
    """A normalised options dict, its vacancies, and its pool.

    Sized the way __init__.create_items sizes it: a boss's ticket check is
    filled in create_regions, so it is not a vacancy and an item sent to it
    would have nowhere to go.
    """
    o = D.normalise(dict(yaml))
    locs = D.all_locations(**D.location_args(o))
    room = len(locs) - sum(1 for l in locs if l.get("type") == "ticket")
    return o, room, D.build_pool(o["mode"], o, room)


def posters_in(pool):
    return (pool["progression"].count(WANTED_POSTER)
            + pool["useful"].count(WANTED_POSTER)
            + pool["filler"].count(WANTED_POSTER))


def _imports_data(D, name):
    """The world's own data file, straight off disk."""
    import json
    with open(os.path.join(WORLD, "data", name), encoding="utf-8") as fh:
        return json.load(fh)


GOAL_NAMES = {0: "wanted_posters", 1: "defeated_bosses", 2: "level_unlock",
              3: "posters_and_bosses", 4: "posters_and_unlock",
              5: "bosses_and_unlock", 6: "all_three"}


def main():
    D = load_logic()
    print("    the item pool, against what a seed can actually use:")
    print()

    # ---- the bug: a requirement of zero means an empty pool ---------------
    inert = []
    for goal in sorted(GOAL_NAMES):
        o, room, pool = seed(D, game_mode="open", goal_conditions=goal)
        if not D.poster_requirement("open", o) and posters_in(pool):
            inert.append(f"{GOAL_NAMES[goal]}: {posters_in(pool)} posters in "
                         f"a seed that requires none")
    check("no Open goal puts a poster in a seed that cannot use one",
          not inert, "; ".join(inert))

    # ---- and the thing that must NOT be broken by fixing it ---------------
    o, room, pool = seed(D, game_mode="open", goal_conditions=0,
                         poster_pool_open=70, goal_posters=50)
    check("a pool above the goal keeps its spares -- 70 for a goal of 50",
          (pool["progression"].count(WANTED_POSTER) == 50
           and pool["useful"].count(WANTED_POSTER) == 20),
          f"{pool['progression'].count(WANTED_POSTER)} progression + "
          f"{pool['useful'].count(WANTED_POSTER)} useful, wanted 50 + 20")

    o, room, pool = seed(D, game_mode="linear")
    check("Linear is untouched: its gates are the requirement",
          posters_in(pool) == D.poster_requirement("linear", o) > 0,
          f"{posters_in(pool)} posters, requirement "
          f"{D.poster_requirement('linear', o)}")

    # ---- the freed slots have to go somewhere -----------------------------
    #
    # Both check types off is the corner that used to overflow: 103 vacancies
    # in Open and 104 items the Wanted Posters goal insists on, which fails
    # the fill with "no more locations" and reads as the world being broken.
    bad = []
    for mode in ("open", "linear"):
        for goal in sorted(GOAL_NAMES):
            for sw, de in ((100, 50), (1, 1), (0, 0), (0, 50), (100, 0)):
                o, room, pool = seed(D, game_mode=mode, goal_conditions=goal,
                                     sandwich_checks=sw, destruction_checks=de)
                if pool["total"] != room:
                    bad.append(f"{mode}/{GOAL_NAMES[goal]}/{sw}/{de}: "
                               f"{pool['total']} items for {room} vacancies")
    check("every seed has exactly one item per vacancy, checks on or off",
          not bad, "; ".join(bad[:3]))

    o, room, pool = seed(D, game_mode="open", goal_conditions=1)
    check("the slots the posters used are filler now, not missing",
          len(pool["filler"])
          == room - len(pool["progression"]) - len(pool["useful"]),
          f"{len(pool['filler'])} filler in a {room} vacancy seed")

    # ---- and the impossible yaml is refused rather than half-built --------
    try:
        D.normalise({"game_mode": "open", "goal_conditions": 0,
                     "sandwich_checks": 0, "destruction_checks": 0},
                    strict=True)
        refused = ""
    except D.TazOptionError as exc:
        refused = str(exc)
    check("a yaml with more items than places says so at generation",
          "places to put them" in refused,
          refused or "generated silently")

    # ---- nothing else inert crept in -------------------------------------
    #
    # Cheap and general: every distinct PROGRESSION item name has to be one
    # some rule can ask for. A name nothing reads is the same bug wearing a
    # different hat.
    strays = []
    catchers = _imports_data(D, "taz_catchers.json")
    for mode in ("open", "linear"):
        for goal in sorted(GOAL_NAMES):
            o, room, pool = seed(D, game_mode=mode, goal_conditions=goal)
            asked = set()

            class Spy(dict):
                """Stands in for the item counts the rules are handed.

                _has does `state.get(item, 0) >= n`, so a dict that writes
                down every key it is asked for and answers "plenty" makes the
                rules themselves say which items matter -- rather than a list
                here that goes stale the next time one is added.
                """

                def get(self, item, default=0):
                    asked.add(item)
                    return 999

            spy = Spy()
            rules = dict(D.entrance_rules(mode, o))
            rules.update(D.location_rules(mode, o, o["difficulty"], catchers))
            rules["<goal>"] = D.goal_rule(mode, o)
            for rule in rules.values():
                rule(spy)

            for name in set(pool["progression"]):
                if name not in asked:
                    strays.append(f"{mode}/{GOAL_NAMES[goal]}: {name}")
    check("every progression item is one some rule actually asks for",
          not strays, "; ".join(sorted(set(strays))[:6]))

    # ---- the client has to count the same thing the rules do --------------
    #
    # A source check, which is crude, but the two numbers live on opposite
    # sides of a boundary these tests cannot import across: the generator
    # requires Wanted Poster ITEMS, and the client used to open the Linear
    # gates on the posters the player had SMASHED. Nothing detects that
    # disagreement -- the seed generates, the game plays, and the gates simply
    # come open on their own while the items meant to open them do nothing.
    #
    # The player's report was the door saying "5 more" while they held 14 of
    # the 15 it wanted.
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    bad_fn = []
    for fn in ("linear_open_bosses", "_door_message"):
        start = src.index(f"def {fn}(")
        end = src.index("\n    def ", start)
        body = src[start:end]
        if "poster_count(" in body:
            bad_fn.append(f"{fn} counts smashed posters")
        elif "self.posters" not in body:
            bad_fn.append(f"{fn} counts neither")
    check("the Linear poster gate counts RECEIVED posters, as the rules do",
          not bad_fn, "; ".join(bad_fn))

    # ...and that the rules really do ask for the ITEM, so the client counting
    # received posters is agreement rather than a second guess.
    o = D.normalise({"game_mode": "linear"})
    asked = set()

    class Spy(dict):
        def get(self, item, default=0):
            asked.add(item)
            return 999

    for rule in D.entrance_rules("linear", o).values():
        rule(Spy())
    check("...and the Linear rules gate on the Wanted Poster item",
          WANTED_POSTER in asked,
          f"the rules ask for {sorted(asked)}")

    # ---- what it looks like now -------------------------------------------
    print()
    print("    Open, by goal:")
    print(f"      {'goal':<20}{'vacancies':>10}{'posters':>9}{'filler':>8}")
    print(f"      {'-' * 20}{'-' * 10}{'-' * 9}{'-' * 8}")
    for goal in sorted(GOAL_NAMES):
        o, room, pool = seed(D, game_mode="open", goal_conditions=goal)
        print(f"      {GOAL_NAMES[goal]:<20}{room:>10}"
              f"{posters_in(pool):>9}{len(pool['filler']):>8}")

    print()
    bad_n = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad_n}/{len(RESULTS)} passed")
    return 1 if bad_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
