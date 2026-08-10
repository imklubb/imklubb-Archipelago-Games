#!/usr/bin/env python3
"""build_apworld.py -- turn worlds/tazwanted/ into dist/tazwanted.apworld.

An .apworld is a plain zip with the world package as a single top-level
folder. Nothing is compiled. The whole job is "zip the right files under the
right prefix and leave out the junk", and the reason it is a script rather
than a line in a README is that all three of those have gone wrong in other
projects:

  * the folder nested one level too deep, so Archipelago finds no world
  * __pycache__ shipped, so the apworld carries bytecode for someone else's
    Python version
  * the version inside archipelago.json disagreeing with the release it was
    attached to, which nobody notices until a bug report cites a version that
    was never built

So this asserts all three on the way out, and `verify` will check any
.apworld you point it at -- including one you did not build here.

    python tools/build_apworld.py              build it
    python tools/build_apworld.py --out X      build it somewhere else
    python tools/build_apworld.py verify F     check an existing .apworld
"""

import argparse
import json
import os
import sys
import zipfile

from taz_paths import ROOT, WORLD

PACKAGE = "tazwanted"

# Never shipped. Bytecode is per-interpreter, and the rest is repository
# furniture that means nothing inside an apworld.
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo"}
SKIP_NAMES = {".gitignore", ".DS_Store", "Thumbs.db"}


def world_files():
    """Every file that belongs in the apworld, as (disk path, zip path)."""
    out = []
    for folder, dirs, files in os.walk(WORLD):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name in SKIP_NAMES or os.path.splitext(name)[1] in SKIP_EXT:
                continue
            disk = os.path.join(folder, name)
            rel = os.path.relpath(disk, WORLD).replace(os.sep, "/")
            out.append((disk, f"{PACKAGE}/{rel}"))
    return out


def world_version():
    with open(os.path.join(WORLD, "archipelago.json"), encoding="utf-8") as f:
        return json.load(f)["world_version"]


def check(path, expect_version=None):
    """Assert an .apworld is shaped the way Archipelago needs. Returns bad."""
    bad = 0

    def ok(good, label, detail=""):
        nonlocal bad
        bad += 0 if good else 1
        print(f"  {'ok  ' if good else 'FAIL'}  {label}"
              + (f"   {detail}" if detail and not good else ""))

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        roots = {n.split("/")[0] for n in names}
        ok(roots == {PACKAGE},
           f"exactly one top-level folder, named {PACKAGE}", sorted(roots))
        ok(f"{PACKAGE}/__init__.py" in names,
           "__init__.py is directly inside it, not nested deeper")
        ok(f"{PACKAGE}/archipelago.json" in names, "archipelago.json is there")
        junk = [n for n in names
                if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
        ok(not junk, "no bytecode or __pycache__", junk[:3])

        meta = json.loads(z.read(f"{PACKAGE}/archipelago.json"))
        version = meta.get("world_version")
        print(f"        game {meta.get('game')!r}, world_version {version!r},"
              f" needs Archipelago {meta.get('minimum_ap_version')}")
        if expect_version is not None:
            ok(version == expect_version,
               f"the version inside matches the source ({expect_version})",
               version)

        # A world with no client is a world nobody can play.
        for needed in ("client.py", "game.py", "logic.py", "TazClient.py"):
            ok(f"{PACKAGE}/{needed}" in names, f"{needed} is present")

        size = os.path.getsize(path)
        print(f"        {len(names)} files, {size / 1e6:.1f} MB")
        big = sorted(z.infolist(), key=lambda i: -i.file_size)[:3]
        for i in big:
            print(f"        largest: {i.file_size / 1e6:5.2f} MB  "
                  f"{i.filename}")
    return bad


def build(out_path):
    files = world_files()
    version = world_version()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for disk, arc in files:
            z.write(disk, arc)
    print(f"\n  built {out_path}")
    print(f"  from {WORLD}")
    print(f"  {len(files)} files\n")
    return check(out_path, expect_version=version), version


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verify", nargs="?", help="an .apworld to check instead")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist",
                                                  f"{PACKAGE}.apworld"))
    args = ap.parse_args()

    if args.verify:
        if not os.path.exists(args.verify):
            print(f"    no such file: {args.verify}")
            return 2
        print(f"\n  checking {args.verify}\n")
        bad = check(args.verify)
    else:
        if not os.path.isdir(WORLD):
            print(f"    no world at {WORLD}")
            return 2
        bad, version = build(args.out)
        if not bad:
            print(f"\n  Ready. Attach this to the {version} release.")

    print()
    if bad:
        print(f"  {bad} check(s) FAILED. Do not ship this.\n")
        return 1
    print("  All checks passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
