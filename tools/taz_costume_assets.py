#!/usr/bin/env python3
"""Which costume models a level actually has in memory.

    py -3.13 taz_costume_assets.py                  ee_dump.bin
    py -3.13 taz_costume_assets.py other_level.bin
    py -3.13 taz_costume_assets.py a.bin b.bin      compare two levels

This is the question that decides whether costumes can be randomised at all.
No emulator needed -- it reads dumps.


WHY IT MATTERS
--------------
`SetCostume` (0x001C9BD0) is the model loader, not just a byte write. It is
a 15-way switch (table 0x0049D170) that calls LoadActor 50 times and
AttachToBone after it, and only writes C_COSTUME at the very end
(0x001CAA28). So a costume that cannot load its models is a costume Taz
does not visibly wear.

Every one of those 50 LoadActor calls passes the same package: $s2, set
once at

    0x001C9C38  addiu $s2, $s2, -0x3b20     ; $s2 = 0x0046C4E0

which is LEVEL_ID_ASCII -- **the current level's name**. And LoadActor
(0x0023C430) resolves that name against the list of ALREADY-LOADED packages
(0x002799C0 walks the list at 0x003FC9D0) and fails soft if the asset is
not there:

    0x0023C4C0  bnel $v0, $zero, ...
    0x0023C4D4  -> "*** WARNING *** Actor %s not found"
    0x00241F54  beqz $s3, ...    AttachToBone bails, returns -1

So if a level's package does not contain a costume's .obe files, granting
that costume gives Taz the id, the ability (dispatch 0x002513A4 -> table
0x004A7A90) and all the AP logic -- and no outfit.

In ee_dump.bin (level 5, safari) there are 55 distinct `costume\*.obe`
names. 48 appear exactly once, at their code-string address in 0x0049Cxxx.
Only five appear again in a resident package directory, and they are the
skater set -- level 5's own costume. Nine packages were loaded at the time
(including a ROOT one named 'taz'); none of them carried another costume.

**One level is not a proof.** Run this on a dump from a different level. If
that level also carries only its own costume, the feature is dead without
ISO work. If any level carries several -- or if a shared package does --
then pointing that ONE instruction at 0x001C9C38 somewhere else is the
whole fix.

Take the second dump with taz_ramdump.py, standing in a different level.
Taz: Haunted (14, werewolf) is a good one: a costume with several parts,
and nothing to do with the skater set.
"""

import os
import re
import struct
import sys

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
DEFAULT = os.path.join(HERE, "ee_dump.bin")

LEVEL_ID_ASCII = 0x0046C4E0     # the level's package name, 8-bit ascii
LEVEL_BYTE = 0x0046DD5C
GAME_STATE = 0x003FF040

# Where SetCostume's own string literals live. A name that appears ONLY in
# here is referenced by code but not resident in any loaded package.
CODE_STRINGS = (0x00490000, 0x004B0000)

COSTUME_RE = re.compile(rb"costume\\[A-Za-z0-9_\- ]+\.obe")

# id -> the name SetCostume's case loads first, for labelling
KNOWN = {
    0x0: "tazninja", 0x1: "tazcowboy", 0x2: "constructionhat",
    0x3: "tazreindeer", 0x4: "explorertaz", 0x5: "tazsurfer",
    0x6: "tazrapper", 0x7: "tazwerewolf", 0x8: "minerpickaxe",
    0x9: "tazindy", 0xA: "taztarzan", 0xB: "tazsnowboarder",
    0xC: "tazswat", 0xD: "tazskater", 0xE: "taztrippy",
}


def load(path):
    if not os.path.exists(path):
        sys.exit(f"  no such dump: {path}")
    with open(path, "rb") as fh:
        d = fh.read()
    if len(d) < 0x02000000:
        sys.exit(f"  {os.path.basename(path)} is {len(d)} bytes, expected 32MB")
    return d


def survey(d):
    """{name: [addresses]} for every costume .obe name in the image"""
    out = {}
    for m in COSTUME_RE.finditer(d):
        out.setdefault(m.group().decode(), []).append(m.start())
    return out


def resident(addrs):
    """an occurrence outside the code-string block means a package has it"""
    return [a for a in addrs if not (CODE_STRINGS[0] <= a < CODE_STRINGS[1])]


def describe(path):
    d = load(path)
    end = d.find(b"\0", LEVEL_ID_ASCII)
    lvl_name = d[LEVEL_ID_ASCII:end].decode("latin-1")
    lid = d[LEVEL_BYTE]
    gs = struct.unpack_from("<I", d, GAME_STATE)[0]
    names = survey(d)
    live = {n: resident(a) for n, a in names.items()}
    live = {n: a for n, a in live.items() if a}
    return {"path": path, "level": lid, "name": lvl_name, "gstate": gs,
            "all": names, "resident": live}


def report(r):
    print(f"  {os.path.basename(r['path'])}")
    print(f"    level {r['level']}  package {r['name']!r}  "
          f"game state {r['gstate']}")
    print(f"    {len(r['all'])} distinct costume .obe names referenced by code")
    print(f"    {len(r['resident'])} of them RESIDENT in a loaded package:")
    if not r["resident"]:
        print("      (none -- if this is a boss or hub level that is expected)")
    for n in sorted(r["resident"]):
        where = " ".join("%08X" % a for a in r["resident"][n])
        print(f"      {n:<36} {where}")
    stems = {n.split("\\")[1].replace(".obe", "") for n in r["resident"]}
    ids = sorted(i for i, s in KNOWN.items() if s in stems)
    if ids:
        print(f"    -> looks like costume id(s): "
              f"{', '.join('0x%X' % i for i in ids)}")
    print()
    return stems


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    paths = argv or [DEFAULT]

    print()
    results = [describe(p) for p in paths]
    sets = [report(r) for r in results]

    if len(results) < 2:
        print("  One dump only. That cannot tell you whether every level")
        print("  packages just its own costume -- run it again on a dump")
        print("  taken in a DIFFERENT level and pass both files.")
        return 0

    print("  " + "-" * 66)
    common = set.intersection(*sets)
    union = set.union(*sets)
    for r, s in zip(results, sets):
        print(f"  level {r['level']:<3} {r['name']:<10} carries {len(s)} "
              f"costume asset(s)")
    print()
    if not common:
        print("  NO costume asset is resident in both levels.")
        print("  Each level packages only what its own booth grants, so a")
        print("  randomised costume would grant the id and the ability and")
        print("  render nothing. The feature needs the assets put into the")
        print("  packages -- ISO work, not something the client can do.")
        return 1
    if len(common) == len(union):
        print("  Both levels carry the SAME costume assets.")
        print("  That means they are in a shared package, and randomising is")
        print("  cheap: repoint the one instruction at 0x001C9C38.")
        return 0
    print(f"  {len(common)} asset(s) in common, {len(union)} across both:")
    for n in sorted(common):
        print(f"      {n}")
    print("\n  Partial overlap. Worth reading before deciding -- it may mean")
    print("  a shared package exists but does not hold everything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
