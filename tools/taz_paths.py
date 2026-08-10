#!/usr/bin/env python3
"""Where everything is, resolved from where THIS file is.

Every taz_*.py tool used to begin:

    HERE = os.path.dirname(os.path.abspath(__file__))
    WORLD = os.path.join(HERE, "worlds", "tazwanted")

which quietly assumed the tool sat in the repo root. Moving them into
`tools/` made `WORLD` point at `tools/worlds/tazwanted`, which does not
exist, and every one of them broke at once.

So `HERE` now means THE REPO ROOT rather than the script's own folder, and
it is found by walking up from this file until `worlds/tazwanted` turns up.
Every `os.path.join(HERE, ...)` in every tool then keeps working, unchanged,
from any depth -- including from the repo root, so this is safe whether the
tools have been moved yet or not.

    ROOT    the repo root: worlds/, ee_dump.bin, data/
    TOOLS   this folder: the tools and the recordings they write

Import it as the tools do:

    from taz_paths import ROOT as HERE, TOOLS

`tools/` is on sys.path automatically when a script inside it is run, so no
path juggling is needed at the call site.
"""

import os

TOOLS = os.path.dirname(os.path.abspath(__file__))


def _find_root(start):
    """Walk up until worlds/tazwanted is found.

    Falls back to the starting folder rather than raising: a tool that then
    cannot find the world says so in its own words, which is a better error
    than an ImportError from here naming a directory the user did not
    mention.
    """
    here = start
    while True:
        if os.path.isdir(os.path.join(here, "worlds", "tazwanted")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return start
        here = parent


ROOT = _find_root(TOOLS)

WORLD = os.path.join(ROOT, "worlds", "tazwanted")
DUMP = os.path.join(ROOT, "ee_dump.bin")
DATA = os.path.join(ROOT, "data")


def tool_file(name):
    """A file the tools own -- a recording, a candidate list, an offset map.

    These live beside the scripts rather than at the repo root, so they moved
    with them. Anything a tool WRITES belongs here.
    """
    return os.path.join(TOOLS, name)


if __name__ == "__main__":
    print(f"  TOOLS  {TOOLS}")
    print(f"  ROOT   {ROOT}")
    print(f"  WORLD  {WORLD}"
          + ("" if os.path.isdir(WORLD) else "   <-- NOT FOUND"))
    print(f"  DUMP   {DUMP}"
          + ("" if os.path.exists(DUMP) else "   (no dump taken yet)"))
