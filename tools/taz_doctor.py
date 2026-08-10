"""
taz_doctor.py -- test each layer separately and say exactly where it breaks.

Put this next to Launcher.py, start PCSX2 with the game loaded into a save
file, then:

    venv\\Scripts\\activate
    python taz_doctor.py

It walks from "can Python see pine" up to "can we write to the game and read
it back", stopping at the first failure. Each step names what it proved, so the
output says where the problem is rather than that there is one.
"""

import importlib
import os
import sys
import traceback

STEP = 0


def step(name):
    global STEP
    STEP += 1
    print(f"\n  {STEP}. {name}")


def ok(msg):
    print(f"     PASS  {msg}")


def fail(msg, fatal=True):
    print(f"     FAIL  {msg}")
    if fatal:
        print("\n  Stopping here: nothing after this can work.\n")
        sys.exit(1)


print("\n  Taz Wanted -- connection doctor")
print("\n  CLOSE THE ARCHIPELAGO CLIENT FIRST.")
print("  PINE allows one connection per slot, so a running client and this")
print("  script cannot both talk to PCSX2 -- the second one times out.")

# ---------------------------------------------------------------------------
step("Check which Python is running")
print(f"     {sys.executable}")
in_venv = (hasattr(sys, "real_prefix")
           or sys.prefix != getattr(sys, "base_prefix", sys.prefix))
try:
    import yaml  # noqa: F401
    has_yaml = True
except ImportError:
    has_yaml = False

if not has_yaml:
    # An activated venv can still hand you the wrong interpreter: the prompt
    # says (venv) while PATH resolves `python` to a system install that has
    # none of Archipelago's packages. Calling the venv's python by path
    # sidesteps it.
    fail("this Python has no yaml, so it is not the one Archipelago uses.\n"
         "           Run it by path instead:\n"
         "               venv\\Scripts\\python.exe taz_doctor.py\n"
         f"           (in a venv: {in_venv})")
ok(f"yaml available, venv = {in_venv}")

# ---------------------------------------------------------------------------
step("Find the world")
folder = os.path.join("worlds", "tazwanted")
if not os.path.isdir(folder):
    fail(f"{folder} does not exist")
ok(folder)

# ---------------------------------------------------------------------------
step("Import the world package")
try:
    world = importlib.import_module("worlds.tazwanted")
    ok(f"game = {world.TazWorld.game!r}")
except Exception:
    traceback.print_exc()
    fail("the package does not import")

# ---------------------------------------------------------------------------
step("Import pine")
pine_path = os.path.join(folder, "pcsx2_interface", "pine.py")
if not os.path.exists(pine_path):
    fail(f"{pine_path} is missing")
try:
    from worlds.tazwanted.pcsx2_interface.pine import Pine
    ok("pine imports")
except Exception:
    traceback.print_exc()
    fail("pine is present but does not import")

# ---------------------------------------------------------------------------
step("Import the memory wrapper")
try:
    from worlds.tazwanted import pcsx2_mem as mem
    ok("pcsx2_mem imports")
except Exception:
    traceback.print_exc()
    fail("pcsx2_mem does not import -- this is why nothing connects")

# ---------------------------------------------------------------------------
step("Check the game layer picked it up")
from worlds.tazwanted import game as G
if G.mem is None:
    fail("game.mem is None, so every read and write is a no-op.\n"
         "           pcsx2_mem imported here but not from inside game.py")
ok("game.mem is set")

# ---------------------------------------------------------------------------
step("Connect to PCSX2")
try:
    hooked = mem.hook()
except Exception:
    traceback.print_exc()
    fail("hook() raised")
if not hooked:
    fail("hook() returned False.\n"
         "           Is PCSX2 running with a game loaded?\n"
         "           Settings > Advanced > Enable PINE, slot 28011.")
ok("hooked")

# ---------------------------------------------------------------------------
step("Identify the game")
try:
    gid = mem.game_id()
    if gid and "20236" in str(gid):
        ok(f"{gid} -- Taz Wanted (NTSC)")
    else:
        print(f"     WARN  game id reads {gid!r}, expected SLUS-20236")
except Exception as e:
    msg = str(e)
    if "timed out" in msg.lower():
        fail("the read timed out.\n"
             "           PINE allows ONE connection per slot, and hook()\n"
             "           succeeding only means the socket opened.\n"
             "           Close the Archipelago client and run this again.")
    print(f"     WARN  could not read the game id: {e}")

# ---------------------------------------------------------------------------
step("Read the basics")
g = G.Game()
g.connected = True
lid = g.level_id()
state = g.game_state()
print(f"     level id    {lid}  ({G.D.LEVEL_IDS.get(lid, 'unknown')})")
if lid is None:
    fail("reads are returning nothing at all. hook() succeeded, so PINE is\n"
         "           connected but the reads are failing -- check that a game\n"
         "           is actually loaded in PCSX2, not just the emulator open.")
print(f"     game state  {state}")
print(f"     in world    {g.in_world()}")
print(f"     demo        {g.demo_running()}")
print(f"     difficulty  {g.difficulty()}")
in_world = g.in_world()
if not in_world:
    print("     NOTE  No save file loaded. That is fine -- the client runs")
    print("           from the title screen and only holds back the writes")
    print("           that touch the save region. The steps below need one,")
    print("           so load a file and run this again to check them.")
else:
    ok("a save file is loaded")

# ---------------------------------------------------------------------------
if not in_world:
    print("\n  Connection is good. Load a save file and run this again to")
    print("  check the save-data steps.\n")
    sys.exit(0)

step("Read save data")
f = g.refresh_save_file()
print(f"     save file   {f + 1}")
print(f"     posters     {g.poster_count()}")
addr = G.D.level_block(5, f) + G.D.L_SANDWICHES
print(f"     Zooney Tunes sandwiches at 0x{addr:06X} = {g._u32(addr)}")
ok("save data reads")

# ---------------------------------------------------------------------------
step("Write, and read it back")
probe = G.T.WARP_DOORS_OPEN
before = g._u32(probe)
g._w32(probe, 1)
after = g._u32(probe)
print(f"     warp doors 0x{probe:06X}: {before} -> {after}")
if after != 1:
    fail("the write did not take. PINE may be read-only, or the address is\n"
         "           wrong for this version of the game.")
ok("writes work")

# ---------------------------------------------------------------------------
step("Check some locations")
from worlds.tazwanted import logic as L
o = L.normalise({})
locs = L.all_locations(catchers=L.D.__dict__.get("CATCHERS") or {},
                       **L.location_args(o))
done = g.satisfied(locs)
print(f"     {len(locs)} locations, {len(done)} currently satisfied")
if done:
    for i in sorted(done)[:5]:
        name = next(x["name"] for x in locs if x["id"] == i)
        print(f"       {name}")
ok("location checking works")

print("\n  Everything the client needs is working.")
print("  If it still does nothing, the problem is in the client loop rather")
print("  than the connection -- say so and include this output.\n")
