#!/usr/bin/env python3
"""Nothing is handed over until the player has picked a save file.

    py -3.13 taz_savefile_test.py

No emulator and no Archipelago. The real `TazPS2.file_selected` runs against
a fake memory, and the real `Client.save_file_prompt` runs against a stub
game, so these test the shipped files rather than a copy of them.

WHY THIS IS WORTH TESTING
-------------------------
The old `save_file()` read 0x003FF2F0 as a u32 and clamped it to 0..2:

    v = self.mem.read_u32(CURRENT_FILE)
    return v if 0 <= v <= 2 else 0

The game stores that as a SIGNED BYTE and parks **-1** in it for "no file
chosen" -- 0x00201E90 and 0x002B85E8 both read it with `lb`. As a u32 with
three zero bytes above it, -1 came back as **255**, fell outside 0..2, and
was reported as **file 0**.

So the title screen was indistinguishable from a loaded file 0, and every
address derived from it -- `level_block(lid, f)`, every completion read,
every enforce_* write -- pointed into a real player's first save. It only
ever appeared to work because nothing ran there, and the moment anything
did it would have written into a file the player never chose.

That is the whole reason these tests exist: the failure is silent, the
symptom is somebody else's save file, and a suite that never puts 0xFF in
that byte stays green through all of it.

The prompt tests are here for a second reason. It talks to the player on
the title screen -- the one place they sit and read the log -- so "said
once" is a correctness property, not a nicety.
"""

import importlib.util
import json
import re
import os
import sys
import tempfile
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

CURRENT_FILE = 0x3FF2F0

PASS, FAIL = [], []


def chk(label, got, want):
    (PASS if got == want else FAIL).append((label, got, want))
    print(f"  {'ok  ' if got == want else 'FAIL'} {label:<52} "
          f"{got!r}" + ("" if got == want else f"   expected {want!r}"))


# ------------------------------------------------------------------ loading

def load_game():
    """game.py, with only its imports replaced."""
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("pcsx2_mem",):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    path = os.path.join(WORLD, "game.py")
    spec = importlib.util.spec_from_file_location("tazworld.game", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tazworld.game"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeMem:
    """Only what file_selected touches. Keep it that way -- a fake that
    quietly answers a method the shipped code did not used to call is how
    a feature ends up doing nothing while the suite stays green."""

    def __init__(self, byte=0):
        self.byte = byte
        self.reads = []

    def read_u8(self, addr):
        self.reads.append(("u8", addr))
        return self.byte

    def read_u32(self, addr):
        raise AssertionError(
            "file_selected read the file byte as a u32 -- that is the bug "
            "this whole file exists for")


# ------------------------------------------------------------------- tests

def test_file_selected(G):
    print("\n  file_selected -- the signed byte the game actually writes\n")
    ps2 = G.TazPS2.__new__(G.TazPS2)

    for raw, want, note in [
        (0x00, 0, "file 0"),
        (0x01, 1, "file 1"),
        (0x02, 2, "file 2"),
        (0xFF, None, "-1, the title screen"),
        (0xFE, None, "-2"),
        (0x03, None, "3, out of range"),
        (0x7F, None, "127"),
        (0x80, None, "-128"),
    ]:
        ps2.mem = FakeMem(raw)
        chk(f"0x{raw:02X} -> {note}", ps2.file_selected(), want)

    ps2.mem = FakeMem(0xFF)
    chk("it is read as ONE byte", ps2.mem.reads or ps2.file_selected() or
        ps2.mem.reads, [("u8", CURRENT_FILE)])

    print("\n  save_file -- still clamped, so no caller computes a wild "
          "address\n")
    for raw, want in ((0x00, 0), (0x02, 2), (0xFF, 0), (0x09, 0)):
        ps2.mem = FakeMem(raw)
        chk(f"save_file() with 0x{raw:02X}", ps2.save_file(), want)

    print("\n  ...but the two now DISAGREE on the title screen, which is "
          "the point\n")
    ps2.mem = FakeMem(0xFF)
    chk("save_file() says 0", ps2.save_file(), 0)
    chk("file_selected() says None", ps2.file_selected(), None)

    print("\n  file_display -- what the player is told\n")
    for raw, want in ((0x00, 1), (0x01, 2), (0x02, 3), (0xFF, None)):
        ps2.mem = FakeMem(raw)
        chk(f"file_display() with 0x{raw:02X}", ps2.file_display(), want)


class StubGame:
    """Just what save_file_prompt asks a game for.

    A stub is the reason this file once passed while the client crashed on
    every connect: `file_selected` was put on TazPS2, the client calls it on
    Game, and THIS class had it -- so the prompt tests were green against an
    object that did not exist. test_client_only_calls_real_game_attrs below
    is the guard for that, and it matters more than anything else here.
    """
    FILE_DISPLAY_BASE = 1

    def __init__(self, picked=None, in_world=True):
        self.picked = picked
        self._in_world = in_world

    def file_selected(self):
        return self.picked

    def in_world(self):
        return self._in_world


class StubClient:
    """The real save_file_prompt, grafted onto the smallest thing that can
    hold it -- so the method under test is the shipped one."""

    def __init__(self, last_file=None):
        self.last_file = last_file
        self._file_prompt = None
        self.saved = 0

    def save_state(self):
        self.saved += 1


def load_prompt():
    """Lift the real save_file_prompt out of client.py source."""
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    start = src.index("    def save_file_prompt(self, g):")
    end = src.index("    def tick(self):", start)
    body = "\n".join(ln[4:] if ln.startswith("    ") else ln
                     for ln in src[start:end].splitlines())
    ns = {}
    exec(body, ns)
    return ns["save_file_prompt"]


def test_prompt():
    print("\n  save_file_prompt -- what the player is told, and how often\n")
    prompt = load_prompt()

    c = StubClient(last_file=None)
    chk("first time on a seed, no file chosen",
        prompt(c, StubGame(None, in_world=False)), ["Select Save File to Begin"])
    chk("  ...and it does not repeat every tick",
        prompt(c, StubGame(None, in_world=False)), [])

    chk("picking file 0 says nothing further",
        prompt(c, StubGame(0)), [])
    chk("  ...and remembers it", c.last_file, 0)
    chk("  ...and persists it once", c.saved, 1)
    chk("  ...but not again on the next tick",
        (prompt(c, StubGame(0)), c.saved)[1], 1)

    chk("back to the title, it now names the file",
        prompt(c, StubGame(None, in_world=False)), ["Load File 1 to Continue"])
    chk("  ...once", prompt(c, StubGame(None, in_world=False)), [])

    c2 = StubClient(last_file=1)
    chk("a remembered file 1 is shown to the player as File 2",
        prompt(c2, StubGame(None, in_world=False)), ["Load File 2 to Continue"])
    c3 = StubClient(last_file=2)
    chk("a remembered file 2 is shown as File 3",
        prompt(c3, StubGame(None, in_world=False)), ["Load File 3 to Continue"])

    print("\n  switching files mid-seed is noticed and re-saved\n")
    c4 = StubClient(last_file=0)
    chk("loading file 2 instead", prompt(c4, StubGame(2)), [])
    chk("  ...updates the memory", c4.last_file, 2)
    chk("  ...and writes the state file", c4.saved, 1)
    chk("  ...so the prompt now names the NEW one",
        prompt(c4, StubGame(None, in_world=False)), ["Load File 3 to Continue"])


def test_state_location():
    """State files live in their own folder, and an old one migrates."""
    print("\n  state files, and the folder they now live in\n")
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    chk("there is a STATE_DIR", 'STATE_DIR = "taz_wanted_states"' in src, True)
    chk("state_path joins it", "os.path.join(STATE_DIR, self.state_name())"
        in src, True)
    chk("load_state migrates first", "load_json(self.migrate_state()" in src,
        True)
    chk("save_state makes the folder", src.count("makedirs(STATE_DIR") >= 2,
        True)

    # Run the real methods against a stub client, from a temp cwd.
    body = src[src.index("    def state_name(self):"):
               src.index("    def load_state(self):")]
    body = "\n".join(l[4:] if l.startswith("    ") else l
                     for l in body.splitlines())
    ns = {"os": os, "STATE_DIR": "taz_wanted_states",
          "STATE_FILE": "taz_client_state.json"}
    exec(body, ns)

    class C:
        seed = "abc123"
    C.state_name = ns["state_name"]
    C.state_path = ns["state_path"]
    C.legacy_state_path = ns["legacy_state_path"]
    C.migrate_state = ns["migrate_state"]
    c = C()

    chk("the path is inside the folder", c.state_path(),
        os.path.join("taz_wanted_states", "taz_client_state_abc123.json"))

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        try:
            os.chdir(d)
            # A player upgrading: their old file is loose in the root.
            with open(c.legacy_state_path(), "w") as f:
                json.dump({"sent": [1, 2, 3], "void_seen": 7}, f)
            got = c.migrate_state()
            chk("an old root file is migrated", got, c.state_path())
            chk("  ...the old one is gone",
                os.path.exists(c.legacy_state_path()), False)
            chk("  ...and nothing was lost",
                json.load(open(got))["void_seen"], 7)
            chk("  migrating again is a no-op", c.migrate_state(),
                c.state_path())

            # A fresh install: nothing anywhere.
            c2 = C()
            c2.seed = "brand_new"
            chk("a seed with no file just gets the new path",
                c2.migrate_state(), c2.state_path())
        finally:
            os.chdir(cwd)


def test_state_roundtrip():
    print("\n  last_file survives the state file\n")
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    chk("save_state writes last_file", '"last_file"' in src, True)
    chk("load_state reads it back", 'd.get("last_file")' in src, True)

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.json")
        json.dump({"sent": [], "catchers": [], "completed": [],
                   "void_seen": 0, "in_game_text": None, "last_file": 2},
                  open(p, "w"))
        chk("a written state file round-trips",
            json.load(open(p)).get("last_file"), 2)


def test_gate_is_wired():
    """The prompt existing is not the feature. The GATE is the feature."""
    print("\n  the tick gate actually consults it\n")
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    tick = src[src.index("    def tick(self):"):]
    chk("tick calls save_file_prompt", "self.save_file_prompt(g)" in tick, True)
    chk("tick returns early without a chosen file",
        "g.file_selected() is None" in tick, True)
    # ...and it must be BEFORE the first save-region write.
    gate = tick.index("g.file_selected() is None")
    for writer in ("refresh_save_file", "enforce_access", "enforce_costumes",
                   "read_completions", "seed_sandwiches"):
        chk(f"  {writer} is behind the gate",
            tick.index(writer) > gate, True)


def test_game_really_has_them(G):
    """The real Game class, not a stub. This is the one that was wrong."""
    print("\n  the REAL Game object answers what the client asks it\n")
    chk("Game.file_selected exists", hasattr(G.Game, "file_selected"), True)
    chk("Game.file_display exists", hasattr(G.Game, "file_display"), True)
    chk("Game.FILE_DISPLAY_BASE exists",
        hasattr(G.Game, "FILE_DISPLAY_BASE"), True)
    chk("TazPS2 has them too", hasattr(G.TazPS2, "file_selected"), True)

    # ...and they actually run, against the module-level mem the real code
    # uses rather than an injected one.
    g = G.Game.__new__(G.Game)
    was = G.mem
    try:
        for raw, want, disp in ((0xFF, None, None), (0x00, 0, 1),
                                (0x02, 2, 3), (0x05, None, None)):
            G.mem = FakeMem(raw)
            chk(f"Game.file_selected() with 0x{raw:02X}",
                g.file_selected(), want)
            chk(f"  Game.file_display() with 0x{raw:02X}",
                g.file_display(), disp)

        class Dead:
            def read_u8(self, a):
                raise OSError("PINE went away")

            def read_u32(self, a):
                raise OSError("PINE went away")

        G.mem = Dead()
        chk("a dead connection reads as 'no file', not a crash",
            g.file_selected(), None)
    finally:
        G.mem = was


def test_client_only_calls_real_game_attrs(G):
    """Every g.<name> in client.py must exist on the real Game.

    The AttributeError that broke every connect would have been caught here
    before it shipped, and this catches the next one for free.
    """
    print("\n  every g.<attr> the client uses exists on Game\n")
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    used = set(re.findall(r"\bg\.([A-Za-z_][A-Za-z0-9_]*)", src))
    # Attributes Game sets on itself in __init__ rather than on the class.
    gsrc = open(os.path.join(WORLD, "game.py"), encoding="utf-8").read()
    on_self = set(re.findall(r"self\.([A-Za-z_][A-Za-z0-9_]*)\s*=", gsrc))
    missing = sorted(n for n in used
                     if not hasattr(G.Game, n) and n not in on_self)
    chk(f"{len(used)} attributes used, none missing", missing, [])


def test_tick_ordering(G):
    """The bonus gate must be patched BEFORE the first map load.

    It decides whether a police box is constructed, and it decides it once,
    while the map is being built. Sitting below the in_world() gate meant it
    could not run until a level was already loaded -- so booting the game,
    picking a file and walking into Yosemite Zoo got a hub built by the
    SHIPPING gate, which answers from the sandwich count. Every level at 100
    sandwiches got a police box regardless of what the server granted, and
    the patch landed a tick later unable to un-build them.

    Ordering is the whole fix, so ordering is what is asserted.
    """
    print("\n  tick() patches the bonus gate before anything can load\n")
    src = open(os.path.join(WORLD, "client.py"), encoding="utf-8").read()
    tick = src[src.index("    def tick(self):"):]

    chk("bonus_gate_tick is called exactly once",
        tick.count("g.bonus_gate_tick("), 1)
    gate = tick.index("g.bonus_gate_tick(")
    for label, needle in (
            ("the demo early-return", "if g.demo_running():"),
            ("the DEMO_SETTLE return", '_demo_until", 0.0)'),
            ("the save-file gate", "g.file_selected() is None"),
            ("the in_world gate", "not g.in_world()"),
    ):
        chk(f"  ...before {label}", gate < tick.index(needle), True)

    # And it must NOT be behind the alive() guard's early return -- that one
    # is correct to sit in front of it, since nothing works without a hook.
    chk("  ...but after the alive() guard",
        gate > tick.index("if not g.alive():"), True)

    chk("a late install warns the player",
        "patched late" in open(os.path.join(WORLD, "game.py"),
                               encoding="utf-8").read(), True)


def main():
    G = load_game()
    print("\n  the file byte is 0x%08X\n" % CURRENT_FILE)
    test_file_selected(G)
    test_game_really_has_them(G)
    test_client_only_calls_real_game_attrs(G)
    test_tick_ordering(G)
    test_prompt()
    test_state_location()
    test_state_roundtrip()
    test_gate_is_wired()

    print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} passed")
    if FAIL:
        for label, got, want in FAIL:
            print(f"    {label}: got {got!r}, expected {want!r}")
        return 1
    print("  Nothing is handed over before a file exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
