"""
Locations.py -- location definitions and the ID table.

Which locations a seed actually contains depends on the options, so the list is
built per seed by taz_data. What has to be stable is the ID for a given name,
and that comes from taz_data's reserved blocks: turning sandwich checks from
every 100 to every 5 fills more of the sandwich block without renumbering
anything else.

The full table below covers every location any seed could contain, because
Archipelago needs one fixed name-to-ID map for the datapackage regardless of
what an individual seed uses.
"""

import json
import os
from typing import Dict, List

from BaseClasses import Location

from . import logic as D


class TazLocation(Location):
    game = "Taz Wanted"


def _catchers() -> dict:
    """The catcher positions, read so it works from a .apworld zip too.

    A path built from __file__ points inside the archive when the world is
    zipped, so os.path.exists is False and the forty-four catcher locations
    quietly vanish from the datapackage.
    """
    from . import _imports
    return _imports.data("taz_catchers.json") or {}


CATCHERS = _catchers()


def _every_location() -> List[dict]:
    """Every location any seed could contain.

    The finest intervals on Expert produce the largest set, and every coarser
    setting is a subset of it: both sandwich and destruction thresholds are
    whole numbers, so a check at 50 exists in the every-1, every-5 and
    every-25 sets with the same id in each.
    Tickets are included because a bosses goal adds them.
    """
    return D.all_locations(sandwich_interval=1, destruction_interval=1,
                           difficulty="expert", catchers=CATCHERS,
                           with_tickets=True)


ALL_LOCATIONS: List[dict] = _every_location()

location_table: Dict[str, int] = {l["name"]: l["id"] for l in ALL_LOCATIONS}
location_def: Dict[str, dict] = {l["name"]: l for l in ALL_LOCATIONS}


def _group(kind: str) -> set:
    return {l["name"] for l in ALL_LOCATIONS if l["type"] == kind}


location_groups: Dict[str, set] = {
    "Wanted Posters": _group("poster"),
    "Sandwiches": _group("sandwich"),
    "Destruction": _group("destruction"),
    "Golden Sam Statues": _group("statue"),
    "Bonus Games": _group("bonus"),
    "Catchers": _group("catcher"),
    "Bosses": _group("boss") | _group("ticket"),
}
for _lid, _name in D.LEVELS:
    location_groups[_name] = {l["name"] for l in ALL_LOCATIONS
                              if l["level"] == _lid}


def locations_for(options: dict) -> List[dict]:
    """The locations a seed with these options contains."""
    from . import logic as O
    return D.all_locations(catchers=CATCHERS, **O.location_args(options))
