"""
Taz Wanted -- Archipelago world.

The logic lives in four modules that know nothing about Archipelago and can be
run on their own: taz_data (locations), taz_items (the pool), taz_rules (the
region graph) and taz_options (the yaml). This file is the adapter between them
and the generator, so the rules can be tested without a server and the client
can share exactly the same code.
"""

from typing import Any, Dict, List

from BaseClasses import ItemClassification as IC, Region, Tutorial
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import (Component, Type, components,
                                       launch_subprocess)

# icon_paths arrived in a later Archipelago than the rest of this API. Missing
# it should cost the client its picture, not stop the world loading.
try:
    from worlds.LauncherComponents import icon_paths
except ImportError:
    icon_paths = None

from . import logic as D
from . import logic as TI
from . import logic as TO
from . import logic as TR
from .Items import (ITEM_BASE_ID, TazItem, classification, item_def,
                    item_groups, item_table)
from .Locations import (CATCHERS, TazLocation, location_def,
                        location_groups, location_table, locations_for)
from .Options import TazOptions, option_groups, slot_data, to_dict


def launch_client(*args):
    from .TazClient import launch_client as launch
    launch_subprocess(launch, name="TazClient")


# The launcher looks icons up by name, so the file is registered before the
# component that refers to it.
if icon_paths is not None:
    icon_paths["taz_client_logo"] = f"ap:{__name__}/TazClient_Logo.png"
    components.append(Component("Taz Wanted Client", func=launch_client,
                                component_type=Type.CLIENT,
                                icon="taz_client_logo"))
else:
    components.append(Component("Taz Wanted Client", func=launch_client,
                                component_type=Type.CLIENT))


class TazWeb(WebWorld):
    theme = "jungle"
    # The categories from the options document, so the yaml and the website
    # read the way they were designed rather than as one flat list.
    option_groups = option_groups
    tutorials = [Tutorial(
        "Multiworld Setup Guide",
        "A guide to playing Taz Wanted with Archipelago.",
        "English", "setup_en.md", "setup/en",
        ["imklubb"],
    )]


class TazWorld(World):
    """Taz is loose in Sam's theme parks, and the levels, costumes and bonus
    games are scattered across the multiworld."""

    game = "Taz Wanted"
    web = TazWeb()
    options_dataclass = TazOptions
    options: TazOptions

    item_name_to_id = item_table
    location_name_to_id = location_table
    item_name_groups = item_groups
    location_name_groups = location_groups

    # --- Universal Tracker ------------------------------------------------
    #
    # UT rebuilds the multiworld to work out what is reachable. By default it
    # needs the player's original yaml, which nobody keeps. Declaring this and
    # implementing interpret_slot_data lets it rebuild from the slot data the
    # server already has, so the tracker works with nothing but a connection.
    ut_can_gen_without_yaml = True

    def interpret_slot_data(self, slot_data: Dict[str, Any]) -> Dict[str, Any]:
        """Restore this world's options from what the server sent.

        The slot data is the already-normalised option dict, so nothing has to
        be re-derived or re-clamped -- that is the reason for sending the
        normalised form rather than the raw yaml.

        `_from_slot_data` matters: the tracker regenerates the world and runs
        generate_early afterwards, which would otherwise read the untouched
        option objects and overwrite all of this with the defaults. The seed
        would then rebuild as a default Open seed regardless of what the player
        actually rolled.
        """
        self.opt = dict(slot_data)
        self.locs = locations_for(self.opt)
        self.starting_levels = slot_data.get("starting_levels_granted", [])
        self._from_slot_data = True
        return slot_data

    def __init__(self, multiworld, player):
        super().__init__(multiworld, player)
        self.opt: Dict[str, Any] = {}
        self.locs: List[dict] = []
        self.pool: Dict[str, list] = {}
        self.starting_levels: List[str] = []
        self._from_slot_data = False

    # ---------------------------------------------------------------- setup

    def generate_early(self) -> None:
        # Universal Tracker regenerates the world and hands the original slot
        # data back through multiworld.re_gen_passthrough. Reading it HERE is
        # what makes the difference: interpret_slot_data alone is not enough,
        # because generate_early runs first and would otherwise rebuild the
        # seed from untouched option objects -- which is why the tracker was
        # evaluating Open rules for a Linear seed.
        passthrough = getattr(self.multiworld, "re_gen_passthrough", None)
        if passthrough and self.game in passthrough:
            self.interpret_slot_data(passthrough[self.game])
            # The starting levels still have to be precollected. Returning
            # here without doing so gave the tracker a world where the player
            # begins with nothing, so it judged things unreachable that the
            # real seed reaches perfectly well -- which is exactly the
            # "expected to be in logic but weren't" it reported.
            #
            # They are taken from the slot data rather than rolled again: a
            # fresh shuffle would pick different levels and disagree just as
            # badly, only less obviously.
            for name in self.starting_levels:
                self.multiworld.push_precollected(self.create_item(name))
            return
        if getattr(self, "_from_slot_data", False):
            return

        self.opt = to_dict(self.options)
        self.locs = locations_for(self.opt)

        # A yaml can ask for something impossible -- a goal above the poster
        # pool, or a destruction start the difficulty does not allow. Those are
        # corrected rather than refused, and said out loud so the player knows
        # the seed is not quite what they asked for.
        for w in self.opt.get("warnings", []):
            print(f"Taz Wanted ({self.player_name}): {w}")

        # Starting levels come out of the pool as precollected, so the run does
        # not open on a single choice.
        if self.opt["mode"] == "open":
            n = int(self.opt.get("starting_levels", 0))
            if n:
                names = [f"{name} Unlock" for _, name in D.LEVELS]
                self.random.shuffle(names)
                self.starting_levels = names[:n]
                for name in self.starting_levels:
                    self.multiworld.push_precollected(self.create_item(name))
            else:
                self.starting_levels = []
        else:
            self.starting_levels = []

    # -------------------------------------------------------------- regions

    def create_regions(self) -> None:
        mode = self.opt["mode"]
        diff = self.opt["difficulty"]
        graph = TR.regions(mode)

        made: Dict[str, Region] = {}
        for name in graph:
            r = Region(name, self.player, self.multiworld)
            made[name] = r
            self.multiworld.regions.append(r)

        rules = TR.entrance_rules(mode, self.opt)

        menu = made[TR.MENU]

        # A region that is another's target is reached THROUGH it, not from
        # the menu. Connecting both ways let Tazland's always-open entrance
        # act as a second, unguarded route into the level itself, so the whole
        # place was in logic from the start.
        inner = {t for name, targets in graph.items() if name != TR.MENU
                 for t in targets if t in made}

        for name, targets in graph.items():
            if name == TR.MENU:
                continue
            if name not in inner:
                rule = rules.get(name)
                menu.connect(made[name], f"To {name}",
                             self._wrap(rule) if rule else None)
            for t in targets:
                if t not in made:
                    continue
                # The rule belongs on this edge: it is the only way in.
                rule = rules.get(t)
                made[name].connect(made[t], f"{name} to {t}",
                                   self._wrap(rule) if rule else None)

        extra = TR.location_rules(mode, self.opt, diff, CATCHERS)
        start_sand = int(self.opt.get("starting_sandwiches", 0))
        for loc in self.locs:
            region = made.get(TR.region_of(loc, mode, diff, start_sand))
            if region is None:
                continue
            l = TazLocation(self.player, loc["name"], loc["id"], region)
            rule = extra.get(loc["name"])
            if rule:
                l.access_rule = self._wrap(rule)
            # A boss's second check IS the ticket, so it is placed here rather
            # than shuffled. It stays a real check the player sends -- it just
            # always pays out the same thing, which makes "defeat N bosses"
            # and "hold N tickets" the same sentence.
            if loc.get("type") == "ticket":
                l.place_locked_item(self.create_item(TR.HINDENBIRD_TICKET))
            region.locations.append(l)

        # In Linear each hub waits on the boss before it, so every boss
        # leaves an event behind saying it was beaten. Placed in the boss's
        # own region, so having it means having reached and cleared it.
        if mode == "linear":
            for bid, ev in TR.BEATEN_EVENT.items():
                region = made.get(
                    TR.BOSS_UNLOCK[bid].replace(" Unlock", ""))
                if region is None:
                    continue
                l = TazLocation(self.player, ev, None, region)
                l.place_locked_item(
                    TazItem(ev, IC.progression, None, self.player))
                region.locations.append(l)

        # Beating Tweety is the goal, so it is an EVENT location: it appears
        # in trackers, becomes reachable exactly when the run is winnable --
        # which is what "go mode" means -- and hands out nothing, because
        # there is nothing left to use an item for.
        #
        # An event has no address, so it is never a check the player sends.
        # That is the difference between showing the goal and rewarding it.
        hb = made.get("The Hindenbird")
        if hb is not None:
            victory = TazLocation(self.player, "BOSS 5: Tweety Defeated",
                                  None, hb)
            victory.access_rule = self._wrap(TR.goal_rule(mode, self.opt))
            victory.place_locked_item(
                TazItem("Victory", IC.progression, None, self.player))
            hb.locations.append(victory)

        self.multiworld.completion_condition[self.player] = \
            lambda state: state.has("Victory", self.player)

    def _wrap(self, rule):
        """Adapt a rule written against a plain dict to a CollectionState.

        The rules are deliberately written without importing Archipelago, so
        they can be tested on their own; this is the only place that knows
        about CollectionState.
        """
        player = self.player

        class _Counts:
            __slots__ = ("state",)

            def __init__(self, state):
                self.state = state

            def get(self, item, default=0):
                return self.state.count(item, player)

        return lambda state: rule(_Counts(state))

    # ----------------------------------------------------------------- items

    def create_item(self, name: str) -> TazItem:
        return TazItem(name, classification(name, progression=True),
                       item_table[name], self.player)

    def create_items(self) -> None:
        mode = self.opt["mode"]
        # A location that already holds something is not a vacancy. Each
        # boss's ticket check is filled in create_regions, so sizing the pool
        # to every location would leave one surplus item per boss with nowhere
        # to go and fail the fill.
        locked = sum(1 for l in self.locs if l.get("type") == "ticket")
        vacancies = len(self.locs) - locked
        pool = TI.build_pool(mode, self.opt, vacancies)
        self.pool = pool

        items: List[TazItem] = []

        prog = list(pool["progression"])
        for name in self.starting_levels:
            if name in prog:
                prog.remove(name)

        for name in prog:
            items.append(TazItem(name, classification(name, True),
                                 item_table[name], self.player))
        for name in pool["useful"]:
            items.append(TazItem(name, classification(name, False),
                                 item_table[name], self.player))

        filler = list(pool["filler"])
        # Precollecting starting levels leaves a gap; fill it rather than
        # letting the counts drift apart.
        short = vacancies - len(items) - len(filler)
        filler += [TI.FALLBACK_FILLER] * max(0, short)

        keep, send = self._split_filler(filler, vacancies, len(items))
        for name in send:
            items.append(TazItem(name, classification(name),
                                 item_table[name], self.player))

        # The counts still balance: every item kept here is one fewer in the
        # pool AND one fewer free location, because it is placed before the
        # fill rather than alongside it.
        self.multiworld.itempool += items
        self._place_local_filler(keep)

    # How much room to leave for progression when Local Filler is on.
    #
    # The filler is locked down BEFORE the fill runs, so whatever is left is
    # everything the fill has to work with -- and it has to put this slot's own
    # progression somewhere logically reachable. Leaving exactly as many free
    # locations as there are progression items is arithmetically correct and
    # would still fail, because the free ones could all be behind the very
    # items waiting to be placed. Four times over is slack enough that the
    # random spread always includes early locations.
    LOCAL_FILLER_HEADROOM = 4

    def _split_filler(self, filler, vacancies, reserved):
        """(kept in this world, sent to the multiworld).

        Traps always travel. A trap nobody else can receive is not a trap, and
        keeping them would quietly turn the trap percentage into a setting
        about this slot only.
        """
        if not self.opt.get("local_filler"):
            return [], filler
        room = vacancies - reserved * self.LOCAL_FILLER_HEADROOM
        if room <= 0:
            # A seed with no sandwich or destruction checks is nearly all
            # progression, so there is no spare filler to keep and the option
            # correctly does nothing.
            return [], filler
        keep, send = [], []
        for name in filler:
            if name not in TI.TRAPS and len(keep) < room:
                keep.append(name)
            else:
                send.append(name)
        return keep, send

    def _place_local_filler(self, names):
        """Lock this slot's own filler into its own locations before the fill.

        The spots are RESERVED rather than the shared pool being constrained
        with local_items. That constraint overflows when a slot has far more
        checks than the ones it is sharing with: its own progression crowds
        its world, leaving fewer open locations than it has filler items --
        which are then forbidden from going anywhere else either.
        """
        if not names:
            return
        free = [l for l in self.multiworld.get_locations(self.player)
                if l.address is not None and l.item is None and not l.locked]
        self.multiworld.random.shuffle(free)
        for name, loc in zip(names, free):
            loc.place_locked_item(TazItem(name, classification(name),
                                          item_table[name], self.player))

    def get_filler_item_name(self) -> str:
        return TI.FALLBACK_FILLER

    # ------------------------------------------------------------- slot data

    def fill_slot_data(self) -> Dict[str, Any]:
        d = slot_data(self.options)
        d["starting_levels_granted"] = self.starting_levels
        return d
