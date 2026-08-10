"""
check_world.py -- find out why a world is not appearing.

Archipelago imports every folder in `worlds/` and quietly skips any that raises.
The symptom is always the same -- "the game doesn't exist" -- regardless of the
cause, so the useful thing is to do the import yourself and read the traceback.

Put this in the root of your Archipelago checkout, next to Launcher.py, and:

    venv\\Scripts\\activate
    python check_world.py tazwanted
"""

import importlib
import os
import sys
import traceback

# An activated venv can still hand you the wrong interpreter: the prompt says
# (venv) while PATH resolves `python` to a system install with none of
# Archipelago's packages. Check before anything else, or the traceback blames
# the world for a missing yaml.
try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  It has no yaml, so it is not the one Archipelago uses.")
    print("  Run it by path instead:")
    print("      venv\\Scripts\\python.exe check_world.py tazwanted\n")
    sys.exit(1)

name = sys.argv[1] if len(sys.argv) > 1 else "tazwanted"
folder = os.path.join("worlds", name)

print()
if not os.path.isdir(folder):
    print(f"  {folder} does not exist.")
    print("  The folder has to be directly inside worlds/, not nested.")
    sys.exit(1)

print(f"  {folder}\n")
for f in sorted(os.listdir(folder)):
    print(f"    {f}")

pine = os.path.join(folder, "pcsx2_interface", "pine.py")
print(f"\n  pine.py present: {os.path.exists(pine)}")

print(f"\n  importing worlds.{name} ...\n")
try:
    mod = importlib.import_module(f"worlds.{name}")
except Exception:
    print("  FAILED. This is why the game does not appear:\n")
    traceback.print_exc()
    sys.exit(1)

world = None
for attr in dir(mod):
    obj = getattr(mod, attr)
    if isinstance(obj, type) and getattr(obj, "game", None) \
            and attr.endswith("World"):
        world = obj
        break

if world is None:
    print("  Imported, but no World class was found.")
    sys.exit(1)

print(f"  imported fine")
print(f"    class          {world.__name__}")
print(f"    game           {world.game!r}")
print(f"    locations      {len(world.location_name_to_id)}")
print(f"    items          {len(world.item_name_to_id)}")

import json
manifest = os.path.join(folder, "archipelago.json")
if os.path.exists(manifest):
    m = json.load(open(manifest, encoding="utf-8"))
    print(f"\n    manifest game  {m.get('game')!r}")
    if m.get("game") != world.game:
        print(f"    MISMATCH -- the build command uses the manifest name,")
        print(f"    so these two have to agree exactly.")
    else:
        print(f"    matches the World class")
    print(f"\n  build it with:")
    print(f'    python Launcher.py "Build APWorlds" -- "{m.get("game")}"')
print()
