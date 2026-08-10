"""
Items.py -- item definitions and the ID table.

The pool itself is built in taz_items, which decides how many of each thing a
seed contains and which posters count as progression. This file gives every
item a stable ID and a classification, and nothing more.

IDS ARE FIXED

Item IDs share taz_data's base and, like location IDs, must never move: a seed
rolled today has to mean the same thing later. So the table below is built from
a fixed list, and new items are appended rather than inserted.
"""

from typing import Dict, List, NamedTuple, Optional

from BaseClasses import Item, ItemClassification as IC

from . import logic as D
from . import logic as TI


class TazItem(Item):
    game = "Taz Wanted"


class ItemDef(NamedTuple):
    name: str
    classification: IC
    # Filler and traps are only ever filler; the rest depends on the seed, so
    # the World adjusts them when it builds the pool.
    fixed: bool = False


def _build() -> List[ItemDef]:
    out: List[ItemDef] = []

    # Level unlocks, Open mode only.
    for _, name in D.LEVELS:
        out.append(ItemDef(f"{name} Unlock", IC.progression))

    # Boss unlocks.
    for name in TI.BOSS_UNLOCKS:
        out.append(ItemDef(name, IC.progression))

    # Costumes and bonus games are shuffled in both modes.
    for name in TI.COSTUMES:
        out.append(ItemDef(name, IC.progression))
    for name in TI.BONUS_UNLOCKS:
        out.append(ItemDef(name, IC.progression))

    # Wanted Posters are the interesting case: as many as the goal needs are
    # progression and the rest are useful, which is decided per seed.
    out.append(ItemDef(TI.WANTED_POSTER, IC.progression))
    out.append(ItemDef(TI.HINDENBIRD_TICKET, IC.progression))

    for name in sorted(TI.FILLER):
        out.append(ItemDef(name, IC.filler, fixed=True))
    for name in sorted(TI.TRAPS):
        out.append(ItemDef(name, IC.trap, fixed=True))

    return out


ITEM_DEFS: List[ItemDef] = _build()

# IDs start well past the locations so the two can never be confused.
ITEM_BASE_ID = D.BASE_ID + 100_000

item_table: Dict[str, int] = {
    d.name: ITEM_BASE_ID + i for i, d in enumerate(ITEM_DEFS)
}
item_def: Dict[str, ItemDef] = {d.name: d for d in ITEM_DEFS}

item_groups: Dict[str, set] = {
    "Level Unlocks": {f"{n} Unlock" for _, n in D.LEVELS},
    "Boss Unlocks": set(TI.BOSS_UNLOCKS),
    "Costumes": set(TI.COSTUMES),
    "Bonus Games": set(TI.BONUS_UNLOCKS),
    "Traps": set(TI.TRAPS),
    "Filler": set(TI.FILLER),
}


def classification(name: str, progression: bool = False) -> IC:
    """What this item counts as in a particular seed.

    Wanted Posters and Hindenbird Tickets are progression only up to the number
    the goal requires; beyond that they are useful. Marking all seventy posters
    as progression makes the generator treat every one as a potential gate,
    which it does not need to do.
    """
    d = item_def.get(name)
    if d is None:
        return IC.filler
    if d.fixed:
        return d.classification
    if name in (TI.WANTED_POSTER, TI.HINDENBIRD_TICKET):
        return IC.progression if progression else IC.useful
    return d.classification
