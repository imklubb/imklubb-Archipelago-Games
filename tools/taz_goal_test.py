#!/usr/bin/env python3
"""What does /goal actually print?

    py -3.13 taz_goal_test.py

No emulator and no Archipelago: CommonClient, Utils and the tracker are stubs,
so the real TazClient.py loads and the real _cmd_goal runs against a scripted
seed. What it logs is captured and asserted on.

WHY THIS IS WORTH TESTING
-------------------------
A status command is the one place a wrong number does the most damage, because
the player will believe it over the game. This world already had a boss door
telling somebody they needed five more posters when they needed one -- the
door was counting the posters they had SMASHED while the gate waits for the
ones they have RECEIVED. Those are different numbers and both exist on the
client.

So every check below is about which number came out, and about not listing a
condition the player's Goal Conditions did not ask for.
"""

import importlib.util
import os
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

LINES = []


class Logger:
    def info(self, msg):
        LINES.append(str(msg))

    warning = error = debug = info


def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class FakeCommandProcessor:
    def __init__(self, ctx=None):
        self.ctx = ctx


class FakeContext:
    pass


def load_client():
    """TazClient.py, with only its Archipelago imports replaced."""
    stub("CommonClient", get_base_parser=None, logger=Logger(),
         server_loop=None, gui_enabled=False,
         ClientCommandProcessor=FakeCommandProcessor,
         CommonContext=FakeContext)
    stub("Utils", async_start=lambda *a, **k: None)
    stub("kvui", GameManager=object)
    for name in ("worlds", "worlds.tracker", "worlds.tracker.TrackerClient"):
        stub(name)

    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    # pcsx2_mem raises on import without pine, and every module that talks to
    # the emulator pulls it in. /goal reads none of it.
    stub("tazworld.pcsx2_mem", hook=lambda *a, **k: False,
         is_hooked=lambda: False)
    stub("pcsx2_mem", hook=lambda *a, **k: False, is_hooked=lambda: False)
    for name in ("_imports", "logic", "game", "notify", "map_view", "client",
                 "TazClient"):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.TazClient"]


class Seed:
    """Just the fields /goal reads, so a scenario is one literal."""

    def __init__(self, mode, opt, posters=0, tickets=0, bosses=()):
        D = sys.modules["tazworld.logic"]
        self.mode = mode
        self.opt = D.normalise(dict(opt, game_mode=mode))
        self.posters = posters
        self.tickets = tickets
        self.bosses = set(bosses)

    # The real ones, so a change to either is caught here too.
    def goal_remaining(self):
        return sys.modules["tazworld.client"].Client.goal_remaining(self)

    def linear_open_bosses(self):
        return sys.modules["tazworld.client"].Client.linear_open_bosses(self)


RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"    {'PASS' if ok else '*** FAIL ***':<12} {label}")
    if detail and not ok:
        print(f"                 {detail}")


def run(TC, seed):
    LINES.clear()
    ctx = FakeContext()
    ctx.logic = seed
    ctx.__class__ = TC.TazContext          # the isinstance the command wants
    TC.TazCommandProcessor(ctx)._cmd_goal()
    return list(LINES)


def joined(lines):
    return "\n".join(lines)


def main():
    TC = load_client()
    print("    /goal, against what the client actually gates on:")
    print()

    # ---- Linear: the gates ARE the answer ---------------------------------
    lines = run(TC, Seed("linear", {
        "gate_elephant_pong": 5, "gate_gladiatoons": 10,
        "gate_dodge_city": 15, "gate_disco_volcano": 20,
    }, posters=14))
    text = joined(lines)

    check("Linear lists all four gates with what each one wants",
          all(f"{n} posters" in text for n in (5, 10, 15, 20)),
          text)
    check("...the ones the player has cleared say OPEN",
          text.count("OPEN") == 2,
          f"{text.count('OPEN')} gates open at 14 posters, wanted 2 (5 and 10)")
    check("...and Dodge City wants ONE more, not five",
          "1 more to go" in text and "5 more to go" not in text,
          text)
    check("Linear does not talk about goal conditions it does not have",
          "Hindenbird Ticket" not in text and "Wanted Posters  " not in text,
          text)

    # ---- the bug this whole command has to not repeat ---------------------
    #
    # A seed where the two numbers differ as far as they can. If /goal ever
    # reads the save instead of the received items, this is what catches it.
    seed = Seed("linear", {"gate_dodge_city": 15,
                           "gate_disco_volcano": 20}, posters=14)

    class Smashed:
        def poster_count(self):
            raise AssertionError("/goal read the posters smashed in game")
    seed.game = Smashed()
    lines = run(TC, seed)
    check("/goal never asks the save how many posters were smashed",
          any("14 Wanted Poster" in l for l in lines), joined(lines))

    # ---- Open: only the conditions the player picked ----------------------
    lines = run(TC, Seed("open", {"goal_conditions": 0, "goal_posters": 50},
                         posters=20))
    text = joined(lines)
    check("Open with a poster goal shows posters and nothing else",
          "Wanted Posters" in text and "Hindenbird Tickets" not in text
          and "Hindenbird Unlock" not in text, text)
    check("...counted from received, with the remainder spelled out",
          "20 of 50" in text and "30 to go" in text, text)

    lines = run(TC, Seed("open", {"goal_conditions": 1, "goal_bosses": 4},
                         tickets=2))
    text = joined(lines)
    check("a bosses goal counts Hindenbird Tickets, not bosses beaten",
          "Hindenbird Tickets" in text and "2 of 4" in text
          and "Wanted Posters" not in text, text)

    lines = run(TC, Seed("open", {"goal_conditions": 6, "goal_posters": 10,
                                  "goal_bosses": 4}, posters=10, tickets=4,
                         bosses={20}))
    text = joined(lines)
    check("all three conditions, all met, reports the fight as open",
          "met, the fight is open" in text
          and text.count("done") == 2 and "received" in text, text)
    check("...and still says the seed is not won until Tweety falls",
          "beat Tweety" in text, text)

    lines = run(TC, Seed("open", {"goal_conditions": 2}, bosses=()))
    text = joined(lines)
    check("a level-unlock goal says so, and says it is missing",
          "The Hindenbird Unlock" in text and "not received yet" in text, text)

    # ---- not connected ----------------------------------------------------
    ctx = FakeContext()
    ctx.logic = None
    ctx.__class__ = TC.TazContext
    LINES.clear()
    TC.TazCommandProcessor(ctx)._cmd_goal()
    check("before a seed is loaded it says so instead of raising",
          any("Not connected" in l for l in LINES), joined(LINES))

    print()
    print("    what a Linear player sees:")
    for line in run(TC, Seed("linear", {
            "gate_elephant_pong": 5, "gate_gladiatoons": 10,
            "gate_dodge_city": 15, "gate_disco_volcano": 20}, posters=14)):
        print(f"      {line}")
    print()
    print("    what an Open player sees:")
    for line in run(TC, Seed("open", {"goal_conditions": 3,
                                      "goal_posters": 50, "goal_bosses": 4},
                             posters=20, tickets=1)):
        print(f"      {line}")

    print()
    bad = RESULTS.count(False)
    print(f"    {len(RESULTS) - bad}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
