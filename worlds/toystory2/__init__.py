import math
import random
import logging
from typing import ClassVar, Dict, List, Optional, Tuple

from BaseClasses import Item, ItemClassification, MultiWorld, Region, Location, Tutorial
from worlds.AutoWorld import WebWorld, World
from Options import OptionError
from .options import ToyStory2Options, ts2_option_groups
from .items import (
    ITEM_TABLE, ToyStory2Item, BASE_ID,
    MOVE_ITEMS, WEAPON_MOVE_ITEMS, TRAVERSAL_MOVE_ITEMS,
    GADGET_ITEMS, MISSING_PART_ITEMS, LEVEL_UNLOCK_ITEMS,
    COIN_LEVEL_UNLOCK_ITEMS, BOSS_UNLOCK_ITEMS,
    COIN_BUNDLE_ITEMS, TRAP_ITEMS, FILLER_ITEMS, MISSING_TOY_ITEMS,
    MISSING_TOY_BUNDLE_ITEMS, TOY_BUNDLE_NAME,
)
from .locations import LOCATION_TABLE, ToyStory2Location, LOC_BASE
from .logic_data import COIN_DATA
from .rules import set_rules, can_access_level

# ── LAUNCHER COMPONENT (custom client) ────────────────────────
# Register a "Toy Story 2 Client" button in the Archipelago Launcher with the TS2
# icon. It launches the same BizHawk integration as the generic client, plus a
# filter that hides your own self->self "Sent" messages. Guarded so that any
# Launcher-API difference across Archipelago versions can't break world loading.
try:
    from worlds.LauncherComponents import (
        Component, components, Type, icon_paths,
    )
    try:
        from worlds.LauncherComponents import launch as _lc_launch  # newer API
    except Exception:
        _lc_launch = None

    # NOTE: the client launch + message filter are defined HERE, in __init__.py,
    # rather than in a separate client.py submodule. AP's frozen/zipped apworld
    # loader loads this __init__ but does NOT make sibling submodules importable
    # via importlib ("No module named 'worlds.toystory2.client'"), so any attempt
    # to import a submodule — eager or lazy — fails. Inlining avoids that entirely.
    # The heavy worlds._bizhawk imports stay inside the functions so they run at
    # click time (in the launcher process, where they ARE importable) and don't
    # cause a circular import at world-load time.

    def _ts2_make_gui_factory(orig_make_gui):
        """Wrap a context's make_gui() so the returned GameManager sets our window
        title."""
        def make_gui(self):
            ui = orig_make_gui(self)  # the stock GameManager class for this context

            class TS2Manager(ui):
                base_title = "Archipelago Toy Story 2 Client"

            return TS2Manager
        return make_gui

    def _install_self_send_filter():
        """Wrap BizHawkClientContext.on_print_json so that items you send to
        YOURSELF (sender == receiver == you) STILL appear in the client log (the
        player needs to see what they received), but skip only the redundant
        in-game (emulator) passthrough line for them. Idempotent: safe to call
        more than once (won't stack wrappers)."""
        from worlds._bizhawk.context import BizHawkClientContext
        from CommonClient import CommonContext
        if getattr(BizHawkClientContext, "_ts2_selfsend_filter", False):
            return
        _orig_on_print_json = BizHawkClientContext.on_print_json

        def on_print_json(self, args):
            try:
                if args.get("type", "") == "ItemSend":
                    receiving = args.get("receiving")
                    item = args.get("item")
                    sender = getattr(item, "player", None)
                    if (receiving is not None and sender is not None
                            and self.slot_concerns_self(receiving)
                            and self.slot_concerns_self(sender)):
                        # Log it to the client (so the player sees the receipt),
                        # but skip BizHawk's in-game passthrough for self-sends.
                        CommonContext.on_print_json(self, args)
                        return
            except Exception:
                pass
            return _orig_on_print_json(self, args)

        BizHawkClientContext.on_print_json = on_print_json
        BizHawkClientContext._ts2_selfsend_filter = True

    def _install_title_wrap():
        """Patch BizHawkClientContext.make_gui to set our window title. Idempotent.
        Needed for the no-Universal-Tracker fallback path (the combined client sets
        its own title directly)."""
        from worlds._bizhawk.context import BizHawkClientContext
        if (hasattr(BizHawkClientContext, "make_gui")
                and not getattr(BizHawkClientContext, "_ts2_gui_wrapped", False)):
            BizHawkClientContext.make_gui = _ts2_make_gui_factory(
                BizHawkClientContext.make_gui)
            BizHawkClientContext._ts2_gui_wrapped = True

    def _ts2_ut_available():
        """True if Universal Tracker (the `tracker` apworld) is importable."""
        try:
            import worlds.tracker.TrackerClient  # noqa: F401
            return True
        except Exception:
            return False

    def _run_ts2_with_tracker(*args):
        """Run a combined BizHawk + Universal Tracker client so the tracker shows
        as an embedded "Tracker Page" tab in this window.

        Mirrors worlds._bizhawk.context.launch's inner main(), but builds a
        context that is BOTH a BizHawkClientContext (real game integration) and a
        TrackerGameContext (UT logic + tab). A single server connection serves
        both. The stock game-watcher and patch helpers are reused as-is, so the
        version-sensitive game-integration internals are not reimplemented here.
        """
        import asyncio
        import Utils
        from CommonClient import get_base_parser, server_loop, gui_enabled, logger
        from worlds._bizhawk.context import (
            BizHawkClientContext, _game_watcher, _patch_and_run_game,
        )
        from worlds.tracker.TrackerClient import (
            TrackerGameContext, TrackerCommandProcessor,
        )

        # Keep BOTH BizHawk's (/bh, /toggle_text) and UT's (/load_map, ...)
        # slash-commands by combining their command processors.
        _CmdProc = type(
            "ToyStory2TrackerCommandProcessor",
            (TrackerCommandProcessor, BizHawkClientContext.command_processor),
            {},
        )

        class ToyStory2TrackerContext(TrackerGameContext, BizHawkClientContext):
            """One connection, two jobs: drives the BizHawk game integration and
            renders UT's in-logic tracker tab at the same time."""
            command_processor = _CmdProc
            # Connect as the real game slot, NOT as a read-only tracker: drop the
            # "Tracker" tag that TrackerGameContext would otherwise add.
            tags = TrackerGameContext.tags - {"Tracker"}

            def __init__(self, server_address, password):
                # TrackerGameContext.__init__ chains into BizHawkClientContext's
                # via super(), so both halves initialise from this one call.
                super().__init__(server_address, password)

            def on_package(self, cmd, args):
                # UT's on_package does NOT call super(), so invoke both explicitly:
                # the game handler first, then the tracker.
                BizHawkClientContext.on_package(self, cmd, args)
                TrackerGameContext.on_package(self, cmd, args)

            async def server_auth(self, password_requested: bool = False):
                # Authenticate using BizHawk's flow (waits for the emulator, takes
                # auth from the ROM handler, normal game connect).
                await BizHawkClientContext.server_auth(self, password_requested)

            def make_gui(self):
                # UT's make_gui wraps the (title-patched) BizHawk manager and adds
                # the "Tracker Page" tab via build(); re-assert our title on top.
                manager_cls = TrackerGameContext.make_gui(self)

                class ToyStory2TrackerManager(manager_cls):
                    base_title = "Archipelago Toy Story 2 Client"

                return ToyStory2TrackerManager

        async def main():
            parser = get_base_parser()
            parser.add_argument("patch_file", default="", type=str, nargs="?",
                                help="Path to an Archipelago patch file")
            pargs = parser.parse_args(args)

            if pargs.patch_file:
                metadata = _patch_and_run_game(pargs.patch_file)
                if "server" in metadata:
                    pargs.connect = metadata["server"]

            ctx = ToyStory2TrackerContext(pargs.connect, pargs.password)
            ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")

            # Build the tracker's simulated world from the player's yaml. A failure
            # here (e.g. no matching yaml in Players/) must NOT take down the game
            # client, so the tab just stays empty.
            try:
                ctx.run_generator()
            except Exception as exc:
                logger.exception(exc)
                logger.error("[TS2] Universal Tracker generation failed; the game "
                             "client still works but the Tracker tab may be empty.")

            if gui_enabled:
                ctx.run_gui()
            ctx.run_cli()

            watcher_task = asyncio.create_task(_game_watcher(ctx), name="GameWatcher")
            try:
                await watcher_task
            except Exception as exc:
                logger.exception(exc)

            await ctx.exit_event.wait()
            await ctx.shutdown()

        Utils.init_logging("ToyStory2Client", exception_logger="Client")
        import colorama
        colorama.just_fix_windows_console()
        asyncio.run(main())
        colorama.deinit()

    def _ts2_launch(*args):
        """Launch the Toy Story 2 client. If Universal Tracker is installed, run a
        combined client with an embedded "Tracker Page" tab; otherwise defer to the
        stock BizHawk client. Either way, apply our window title and the self->self
        "Sent" filter."""
        import logging
        _install_self_send_filter()
        _install_title_wrap()
        if _ts2_ut_available():
            try:
                _run_ts2_with_tracker(*args)
                return
            except Exception:
                logging.getLogger("Client").exception(
                    "[TS2] Universal Tracker integration failed to start; falling "
                    "back to the standard BizHawk client.")
        from worlds._bizhawk.context import launch as bizhawk_launch
        bizhawk_launch(*args)

    def _launch_ts2_client(*args):
        import logging
        try:
            if _lc_launch is not None:
                _lc_launch(_ts2_launch, name="Toy Story 2 Client", args=args)
            else:
                _ts2_launch(*args)
        except Exception:
            logging.getLogger("Client").exception("[TS2] Failed to launch client")
            raise

    # Register the icon shipped inside this apworld. Derive the path from THIS
    # package's module name so it resolves regardless of where the apworld is
    # installed. The "ap:" prefix tells the launcher to resolve inside the package.
    icon_paths["ts2_logo"] = f"ap:{__name__}/TS2_Client_Logo.png"

    components.append(Component(
        "Toy Story 2 Client",
        func=_launch_ts2_client,
        component_type=Type.CLIENT,
        icon="ts2_logo",
        description="Connect to a Toy Story 2 (PS1) Archipelago game via BizHawk.",
    ))
    import logging as _logging
    _logging.getLogger("Client").info(
        "[TS2] Registered Toy Story 2 Client launcher component (pkg=%s)", __name__)
except Exception:
    # Launcher integration is optional; never let it block the world from loading,
    # but DO log why it failed so we can diagnose (otherwise the button silently
    # never appears).
    import logging as _logging
    _logging.getLogger("Client").exception(
        "[TS2] Failed to register Toy Story 2 Client launcher component")

# ── COIN LEVEL INFO ───────────────────────────────────────────

COIN_LEVELS = [
    "Andy's House",
    "Andy's Neighborhood",
    "Construction Yard",
    "Alleys and Gullies",
    "Al's Toy Barn",
    "Al's Space Land",
    "Elevator Hop",
    "Al's Penthouse",
    "Airport Infiltration",
    "Tarmac Trouble",
]

ALL_LEVELS = [
    "Andy's House", "Andy's Neighborhood", "Bombs Away!",
    "Construction Yard", "Alleys and Gullies", "Slime Time",
    "Al's Toy Barn", "Al's Space Land", "Toy Barn Encounter",
    "Elevator Hop", "Al's Penthouse", "The Evil Emperor Zurg",
    "Airport Infiltration", "Tarmac Trouble", "Prospector Showdown",
]

BOSS_LEVELS = [
    "Bombs Away!", "Slime Time", "Toy Barn Encounter",
    "The Evil Emperor Zurg", "Prospector Showdown",
]

# Gadgets always in the pool regardless of movesanity
ALL_GADGET_ITEMS = list(GADGET_ITEMS)

# ── TRAP WEIGHTS ──────────────────────────────────────────────

TRAP_WEIGHT_VALUES = {0: 0, 1: 1, 2: 3, 3: 6}  # Off/Low/Medium/High


class ToyStory2Web(WebWorld):
    theme = "ocean"
    option_groups = ts2_option_groups
    tutorials = [Tutorial(
        "Multiworld Setup Guide",
        "A guide to setting up Toy Story 2 for Archipelago.",
        "English",
        "setup_en.md",
        "setup/en",
        ["You!"]
    )]


class ToyStory2World(World):
    """
    Buzz Lightyear is on a mission to rescue Woody from the evil Al McWhiggin!
    Explore 15 levels, collect Pizza Planet Tokens, and defeat the Prospector
    to save Woody and infinity and beyond!
    """
    game: ClassVar[str] = "Toy Story 2"
    web: ClassVar[WebWorld] = ToyStory2Web()
    options_dataclass = ToyStory2Options
    options: ToyStory2Options

    # Universal Tracker: opt in to yaml-less tracking. With this set, UT writes a
    # template (empty-options) yaml for the connected slot so its generator has a
    # player, then interpret_slot_data + the re_gen_passthrough restore in
    # generate_early override every option from slot_data to match the real seed.
    # Without this flag UT requires the player's yaml in the Players folder.
    ut_can_gen_without_yaml: ClassVar[bool] = True

    # Built at class level — includes ALL possible locations and items
    # Dynamic ones (coin bundles) are added in create_regions
    item_name_to_id: ClassVar[Dict[str, int]] = {
        name: data.code for name, data in ITEM_TABLE.items() if data.code is not None
    }

    # Base location table — coin bundle locations are added dynamically
    location_name_to_id: ClassVar[Dict[str, int]] = {
        name: data.code for name, data in LOCATION_TABLE.items() if data.code is not None
    }

    # ── COIN BUNDLE LOCATION IDS ──────────────────────────────
    # Pre-register all possible coin bundle locations
    # Max bundles per level: ceil(103/1) = 103 (Alleys, largest level)
    # We register up to 110 per level to be safe, offset per level
    _COIN_BUNDLE_OFFSET = LOC_BASE + 2000
    _COIN_BUNDLE_PER_LEVEL = 110

    @classmethod
    def _coin_bundle_id(cls, level_idx: int, bundle_num: int) -> int:
        return cls._COIN_BUNDLE_OFFSET + (level_idx * cls._COIN_BUNDLE_PER_LEVEL) + bundle_num

    # Add coin bundle IDs to location_name_to_id at class level
    for _li, _level in enumerate(COIN_LEVELS):
        _coins = COIN_DATA.get(_level, [])
        # Register max possible bundles (bundle size 1 = 1 location per coin)
        for _bn in range(1, len(_coins) + 1):
            _loc_name = f"{_level} - Coin Bundle {_bn}"
            location_name_to_id[_loc_name] = _COIN_BUNDLE_OFFSET + (_li * _COIN_BUNDLE_PER_LEVEL) + _bn

    # ── DESCRIPTIVE COIN LOCATION IDS ─────────────────────────
    # When coinsanity_checks_bundle_size == 1, each coin is its own AP check
    # using its descriptive name (e.g. "Andy's House - Andy's Room - On Chair -
    # Coin") instead of a "Coin Bundle N" milestone. Register a stable ID for
    # every coin so the datapackage covers both modes. The ID is keyed by the
    # coin's 1-based in-game index per level and sits well clear of bundle IDs.
    _COIN_DESC_OFFSET = LOC_BASE + 5000
    _COIN_DESC_PER_LEVEL = 150

    @classmethod
    def _coin_desc_id(cls, level_idx: int, coin_idx: int) -> int:
        return cls._COIN_DESC_OFFSET + (level_idx * cls._COIN_DESC_PER_LEVEL) + coin_idx

    for _li, _level in enumerate(COIN_LEVELS):
        for _c in COIN_DATA.get(_level, []):
            location_name_to_id[_c.name] = _COIN_DESC_OFFSET + (_li * _COIN_DESC_PER_LEVEL) + _c.idx

    required_client_version: Tuple[int, int, int] = (0, 5, 0)

    item_name_groups = {
        "Moves":            frozenset(MOVE_ITEMS),
        "Weapon Moves":     frozenset(WEAPON_MOVE_ITEMS),
        "Traversal Moves":  frozenset(TRAVERSAL_MOVE_ITEMS),
        "Gadgets":          frozenset(GADGET_ITEMS),
        "Missing Parts":    frozenset(MISSING_PART_ITEMS),
        "Missing Toys":     frozenset(MISSING_TOY_ITEMS) | frozenset(MISSING_TOY_BUNDLE_ITEMS),
        "Level Unlocks":    frozenset(LEVEL_UNLOCK_ITEMS),
        "Coin Bundles":     frozenset(COIN_BUNDLE_ITEMS),
        "Traps":            frozenset(TRAP_ITEMS),
        "Filler":           frozenset(FILLER_ITEMS),
    }

    def __init__(self, multiworld: MultiWorld, player: int):
        super().__init__(multiworld, player)
        self.coin_bundle_locations: List[str] = []  # populated in create_regions

    # ── STANDARD ITEM CREATION (required by AP core + Universal Tracker) ──
    def create_item(self, name: str) -> ToyStory2Item:
        """Create an item by name. AP core and Universal Tracker call this, so it
        must resolve every item name in item_name_to_id (previously only the
        internal _make_item existed, which UT can't see — hence the 'not able to
        be created' errors)."""
        data = ITEM_TABLE[name]
        return ToyStory2Item(name, data.classification, data.code, self.player)

    def create_event(self, name: str) -> ToyStory2Item:
        """Create a non-networked event item (no code)."""
        return ToyStory2Item(name, ItemClassification.progression, None, self.player)

    # ── HELPERS ───────────────────────────────────────────────

    def _make_item(self, name: str, override_class: Optional[ItemClassification] = None) -> ToyStory2Item:
        if override_class is None:
            return self.create_item(name)
        data = ITEM_TABLE[name]
        return ToyStory2Item(name, override_class, data.code, self.player)

    def _is_coinsanity(self) -> bool:
        return bool(self.options.coinsanity.value)

    def _is_movesanity(self) -> bool:
        return self.options.movesanity.value != 0

    def _checks_bundle_size(self) -> int:
        return self.options.coinsanity_checks_bundle_size.value  # 0 = ALL

    def _received_bundle_size(self) -> int:
        return self.options.coinsanity_received_bundle_size.value

    def _num_check_bundles(self, level: str) -> int:
        """How many coin-bundle CHECK locations a level has = level coin total
        divided by the checks bundle size."""
        coins = COIN_DATA.get(level, [])
        total = len(coins)
        if total == 0:
            return 0
        size = self._checks_bundle_size()
        if size == 0:  # ALL -> one check for the whole level
            return 1
        return math.ceil(total / size)

    def _num_received_bundles(self, level: str) -> int:
        """How many coin-bundle ITEMS a level contributes = level coin total
        divided by the received bundle size (which is never 'all'). Splitting
        these from the check count frees pool slots for Pizza Planet Tokens."""
        coins = COIN_DATA.get(level, [])
        total = len(coins)
        if total == 0:
            return 0
        size = self._received_bundle_size()
        if size <= 0:
            return 1
        return math.ceil(total / size)

    def _is_open_mode(self) -> bool:
        return self.options.game_mode.value == 0

    def _trap_pool(self) -> List[str]:
        """Build weighted trap list based on settings."""
        traps = []
        weights = {
            "Cutscene Trap":            self.options.cutscene_trap_weight.value,
            "Narrow Vision Trap":       self.options.narrow_vision_trap_weight.value,
            "Damage Buzz Trap":         self.options.damage_buzz_trap_weight.value,
            "Freeze Buzz Trap":         self.options.freeze_buzz_trap_weight.value,
            "Invincible Enemies Trap":  self.options.invincible_enemies_trap_weight.value,
            "Dizzy Buzz":               self.options.dizzy_buzz_trap_weight.value,
        }
        for trap_name, weight in weights.items():
            traps.extend([trap_name] * TRAP_WEIGHT_VALUES[weight])
        return traps

    def _filler_pool(self) -> List[str]:
        """Build a weighted filler list based on settings, mirroring traps."""
        filler = []
        weights = {
            "1 Life":        self.options.one_life_filler_weight.value,
            "Extra Battery": self.options.extra_battery_filler_weight.value,
            "Invincible Buzz": self.options.invincible_buzz_filler_weight.value,
            "Determination to Save Woody": self.options.determination_filler_weight.value,
        }
        for name, weight in weights.items():
            filler.extend([name] * TRAP_WEIGHT_VALUES[weight])
        # If EVERY filler is set to Off, Archipelago would have nothing to fill
        # empty locations with. "Determination to Save Woody" does nothing when
        # received (pure flavor), so force it to High as the safe fallback.
        if not filler:
            filler = ["Determination to Save Woody"] * TRAP_WEIGHT_VALUES[3]
        return filler

    def _get_filler_or_trap(self) -> str:
        trap_pct = self.options.filler_replaced_with_traps.value
        trap_pool = self._trap_pool()
        if trap_pool and self.multiworld.random.randint(1, 100) <= trap_pct:
            return self.multiworld.random.choice(trap_pool)
        return self.multiworld.random.choice(self._filler_pool())

    # ── GENERATE EARLY ────────────────────────────────────────

    def generate_early(self) -> None:
        options = self.options

        # Universal Tracker: during its logic re-generation, re_gen_passthrough
        # carries the real seed's slot_data. Restore the options that drive logic
        # so the tracker matches the actual game even if the player's yaml has
        # since changed (skips re-tiers every Easy/Hard check, game_mode and the
        # gates change level access).
        _passthrough = getattr(self.multiworld, "re_gen_passthrough", None)
        _ut_sd = _passthrough.get("Toy Story 2") if isinstance(_passthrough, dict) else None
        if isinstance(_ut_sd, dict):
            # Restore EVERY option carried in slot_data so UT's regeneration
            # matches the real seed even with no player yaml in the Players
            # folder. Looping over the slot_data keys (rather than a hand-kept
            # list) means any option added to fill_slot_data later — including the
            # upcoming coinsanity ones — is picked up automatically.
            # starting_levels is a derived list, not an option, applied below.
            for _opt, _val in _ut_sd.items():
                if _opt == "starting_levels":
                    continue
                _attr = getattr(options, _opt, None)
                if _attr is not None and hasattr(_attr, "value"):
                    try:
                        _attr.value = _val
                    except Exception:
                        pass

        # In open mode, validate starting levels vs pool size
        if self._is_open_mode():
            pool = list(COIN_LEVELS)
            if options.omit_airport_infiltration.value:
                pool = [l for l in pool if l != "Airport Infiltration"]
            if options.omit_elevator_hop.value:
                pool = [l for l in pool if l != "Elevator Hop"]
            starting = min(options.starting_levels.value, len(pool))

            # Universal Tracker re-generates the world to derive logic, but it
            # can't reproduce our random starting-level pick. When UT hands the
            # real seed's slot_data back through re_gen_passthrough, use the
            # ACTUAL starting levels from it so the tracker's reachability matches
            # the real game (otherwise it re-rolls different starting levels and
            # thinks unrelated levels are free).
            passthrough = getattr(self.multiworld, "re_gen_passthrough", None)
            chosen = None
            if isinstance(passthrough, dict):
                sd = passthrough.get("Toy Story 2")
                if isinstance(sd, dict) and sd.get("starting_levels"):
                    chosen = [lv for lv in sd["starting_levels"] if lv in pool]
            if chosen:
                self._starting_levels = chosen
            else:
                self._starting_levels = self.multiworld.random.sample(pool, starting)
        else:
            self._starting_levels = ["Andy's House", "Andy's Neighborhood"]

    # ── CREATE REGIONS ────────────────────────────────────────

    def create_regions(self) -> None:
        menu = Region("Menu", self.player, self.multiworld)
        self.multiworld.regions.append(menu)

        options = self.options
        skips = options.skips.value

        for level_name in ALL_LEVELS:
            region = Region(level_name, self.player, self.multiworld)
            self.multiworld.regions.append(region)

            # Connect menu to level (access rules applied in set_rules)
            menu.connect(region, f"To {level_name}")

            # Add non-coin locations
            for loc_name, loc_data in LOCATION_TABLE.items():
                if loc_data.region != level_name:
                    continue
                # Filter by option
                if loc_data.option == "coinsanity" and not self._is_coinsanity():
                    continue
                if loc_data.option == "lifesanity" and not options.lifesanity.value:
                    continue
                if loc_data.option == "batterysanity" and not options.batterysanity.value:
                    continue
                if loc_data.option == "green_laser_sanity" and not options.green_laser_sanity.value:
                    continue
                if loc_data.option == "rexsanity" and not options.rexsanity.value:
                    continue
                if loc_data.option == "hint_block_sanity" and not options.hint_block_sanity.value:
                    continue

                loc = ToyStory2Location(self.player, loc_name, loc_data.code, region)
                region.locations.append(loc)

            # Add coin CHECK locations. In 1-coin mode (checks bundle size == 1)
            # each coin is its own descriptive location; otherwise coins are
            # grouped into "Coin Bundle N" milestone locations.
            if self._is_coinsanity() and level_name in COIN_LEVELS:
                level_idx = COIN_LEVELS.index(level_name)
                if self._checks_bundle_size() == 1:
                    for _c in COIN_DATA.get(level_name, []):
                        loc_id = self._coin_desc_id(level_idx, _c.idx)
                        loc = ToyStory2Location(self.player, _c.name, loc_id, region)
                        region.locations.append(loc)
                        self.coin_bundle_locations.append(_c.name)
                else:
                    num_bundles = self._num_check_bundles(level_name)
                    for bn in range(1, num_bundles + 1):
                        loc_name = f"{level_name} - Coin Bundle {bn}"
                        loc_id = self._coin_bundle_id(level_idx, bn)
                        loc = ToyStory2Location(self.player, loc_name, loc_id, region)
                        region.locations.append(loc)
                        self.coin_bundle_locations.append(loc_name)

        # Place goal event
        prospector = self.multiworld.get_region("Prospector Showdown", self.player)
        goal_loc = ToyStory2Location(
            self.player, "Prospector Showdown - Defeat GOAL", None, prospector
        )
        goal_loc.place_locked_item(
            ToyStory2Item("Victory", ItemClassification.progression, None, self.player)
        )
        prospector.locations.append(goal_loc)
        self.multiworld.completion_condition[self.player] = \
            lambda state: state.has("Victory", self.player)

        # ── FINAL SHOWDOWN TICKETS (hard-coded boss rewards) ──
        # Reward 1 of every non-final boss is ALWAYS a Final Showdown Ticket —
        # these are NOT randomized. Defeating a boss hands the player a ticket,
        # and the client counts received Final Showdown Ticket items to know how
        # many bosses have been beaten. Both modes need this: open mode gates the
        # final showdown on ticket count, and linear mode uses tickets to know
        # which areas are unlocked.
        for boss_reward1 in (
            "Bombs Away! - Defeat Reward 1",
            "Slime Time - Defeat Reward 1",
            "Toy Barn Encounter - Defeat Reward 1",
            "The Evil Emperor Zurg - Defeat Reward 1",
        ):
            loc = self.multiworld.get_location(boss_reward1, self.player)
            loc.place_locked_item(self._make_item("Final Showdown Ticket"))

    # ── CREATE ITEMS ──────────────────────────────────────────

    def create_items(self) -> None:
        options = self.options
        items_to_add: List[ToyStory2Item] = []

        # ── MOVES ─────────────────────────────────────────────
        movesanity = options.movesanity.value
        if movesanity == 0:
            # No movesanity — all moves pre-collected (handled via start_inventory)
            pass
        elif movesanity == 1:
            # Full movesanity — all moves in pool
            for move in MOVE_ITEMS:
                if move in ("Progressive Laser", "Progressive Spin"):
                    # Only level 1 is required for logic; levels 2-3 are pure
                    # upgrades, so the first copy is progression and the other two
                    # are marked useful to free up the fill.
                    for i in range(3):
                        items_to_add.append(self._make_item(
                            move,
                            override_class=(ItemClassification.useful if i else None)))
                else:
                    items_to_add.append(self._make_item(move))
        elif movesanity == 2:
            # LITE Weapons
            for move in WEAPON_MOVE_ITEMS:
                if move in ("Progressive Laser", "Progressive Spin"):
                    # Only level 1 is required for logic; levels 2-3 are pure
                    # upgrades, so the first copy is progression and the other two
                    # are marked useful to free up the fill.
                    for i in range(3):
                        items_to_add.append(self._make_item(
                            move,
                            override_class=(ItemClassification.useful if i else None)))
                else:
                    items_to_add.append(self._make_item(move))
        elif movesanity == 3:
            # LITE Traversal
            for move in TRAVERSAL_MOVE_ITEMS:
                items_to_add.append(self._make_item(move))

        # ── GADGETS ───────────────────────────────────────────
        for gadget in ALL_GADGET_ITEMS:
            items_to_add.append(self._make_item(gadget))

        # ── MISSING PARTS ─────────────────────────────────────
        for part in MISSING_PART_ITEMS:
            items_to_add.append(self._make_item(part))

        # ── MISSING TOYS (5 progressive of each, both modes) ──
        # The 50 toy locations are always in the pool. Individual mode adds the
        # matching 50 toy items (5 per type); bundle mode adds 10 (one "5 X" per
        # level) and the freed slots become filler. The client counts received and
        # writes the count to SHARED_TOY_RECEIVED for that level.
        bundle5 = (options.missing_toy_bundle_size.value == 5)
        for toy_item in MISSING_TOY_ITEMS:
            if bundle5:
                # One item grants all 5 of the level's toys. The 40 toy-item
                # slots this frees up are taken up by filler in the balance step.
                items_to_add.append(self._make_item(TOY_BUNDLE_NAME[toy_item]))
            else:
                for _ in range(5):
                    items_to_add.append(self._make_item(toy_item))

        # ── PIZZA PLANET TOKENS ───────────────────────────────
        # Deferred to the balance step: the requested pool size is capped to the
        # number of locations that actually remain after every other required
        # item is placed, so a high token request with low (few-location) settings
        # can't overflow the pool (which previously silently truncated the count).
        token_count_requested = options.pizza_planet_token_pool.value

        # ── FINAL SHOWDOWN TICKETS ────────────────────────────
        # Not added to the randomized pool: in open mode they are hard-coded as
        # locked items on each non-final boss's Reward 1 (placed in
        # create_regions). In linear mode tickets aren't items — gates are
        # token-based.

        # ── LEVEL UNLOCKS (Open Mode only) ───────────────────
        if self._is_open_mode():
            starting = set(self._starting_levels)
            # The Final Showdown Unlock item only matters when the goal includes
            # the "level unlock" condition; otherwise the Prospector is gated by
            # tokens/bosses (in both logic and game), so the item would be dead.
            goal_needs_unlock = options.goal_conditions.value in (2, 4, 5, 6)
            for level_unlock in LEVEL_UNLOCK_ITEMS:
                if level_unlock == "Final Showdown Unlock":
                    if not goal_needs_unlock:
                        continue  # gated by tokens/bosses, not an item
                    level_name = "Prospector Showdown"
                else:
                    level_name = level_unlock.replace(" Unlock", "")
                if level_name in starting:
                    # Starting levels are pre-collected
                    self.multiworld.push_precollected(self._make_item(level_unlock))
                else:
                    items_to_add.append(self._make_item(level_unlock))

        # ── COIN BUNDLES (items: count uses the received bundle size) ──
        # Only the bundles needed for Hamm's 50-coin token are progression; the
        # rest (coins past 50) are useful. This mirrors hamms_50_coins_rule
        # (needs ceil(50/recv) bundles, and no Hamm if the level has < 50 coins)
        # and eases generation by shrinking the progression pool.
        if self._is_coinsanity():
            recv_size = options.coinsanity_received_bundle_size.value or 5
            for level_name in COIN_LEVELS:
                num_bundles = self._num_received_bundles(level_name)
                total_coins = len(COIN_DATA.get(level_name, []))
                bundle_item_name = f"Coin Bundle - {level_name}"
                if total_coins >= 50:
                    prog_count = min(math.ceil(50 / recv_size), num_bundles)
                else:
                    prog_count = 0
                for i in range(num_bundles):
                    cls = (ItemClassification.progression if i < prog_count
                           else ItemClassification.useful)
                    items_to_add.append(self._make_item(bundle_item_name, cls))

        # ── PRE-COLLECT MOVES IF NO MOVESANITY ───────────────
        if movesanity == 0:
            for move in MOVE_ITEMS:
                if move == "Progressive Laser":
                    # Laser is NOT randomized in this mode, so grant only the
                    # first laser. Progressive Laser 2 and 3 are upgrades meant to
                    # be found through Movesanity / LITE Weapons; granting all 3
                    # here would rob those modes of their progression.
                    self.multiworld.push_precollected(self._make_item("Progressive Laser"))
                else:
                    self.multiworld.push_precollected(self._make_item(move))
        elif movesanity == 2:
            # LITE Weapons — pre-collect traversal moves
            for move in TRAVERSAL_MOVE_ITEMS:
                self.multiworld.push_precollected(self._make_item(move))
        elif movesanity == 3:
            # LITE Traversal — pre-collect weapon moves. Laser isn't randomized in
            # this mode either, so grant only the first Progressive Laser.
            for move in WEAPON_MOVE_ITEMS:
                if move == "Progressive Laser":
                    self.multiworld.push_precollected(self._make_item("Progressive Laser"))
                else:
                    self.multiworld.push_precollected(self._make_item(move))

        # ── BALANCE: TOKENS (capped) then FILLER/TRAPS ────────
        # Count available (unfilled, non-event) locations. Locations that already
        # hold a locked item (e.g. the hard-coded Final Showdown Tickets on boss
        # Reward 1 slots, and the Victory goal) must be excluded.
        loc_count = len([l for l in self.multiworld.get_locations(self.player)
                         if not l.is_event and l.item is None])
        item_count = len(items_to_add)
        free_slots = loc_count - item_count

        # GUARD: the required items (coin-bundle items, level unlocks, moves,
        # missing parts, etc.) must fit in the available check locations. The
        # classic way to violate this is a lopsided Coinsanity config — e.g.
        # CHECKS bundle size = ALL (one check per level) but RECEIVED bundle size
        # = 1 (one coin ITEM per coin): that produces hundreds of coin-bundle
        # items but only ~10 coin-bundle check locations to hold them, so the
        # required items can't fit and Fill would die with an opaque error. Fail
        # early here with an actionable message instead.
        if free_slots < 0:
            over = -free_slots
            hint = ""
            if self._is_coinsanity():
                cb = self._checks_bundle_size()
                rb = self._received_bundle_size()
                total_check_bundles = sum(self._num_check_bundles(l) for l in COIN_LEVELS)
                total_recv_bundles = sum(self._num_received_bundles(l) for l in COIN_LEVELS)
                if total_recv_bundles > total_check_bundles:
                    hint = (
                        f" This looks like a lopsided Coinsanity setup: your "
                        f"'received' bundle size ({'1 coin' if rb<=0 else rb}) produces "
                        f"{total_recv_bundles} coin-bundle ITEMS, but your 'checks' "
                        f"bundle size ({'ALL' if cb==0 else cb}) only creates "
                        f"{total_check_bundles} coin-bundle CHECK location(s) to hold "
                        f"them. Make the checks bundle size smaller (more check "
                        f"locations) and/or the received bundle size larger (fewer "
                        f"items), or enable more sanities to add locations."
                    )
            raise OptionError(
                f"[Toy Story 2] Player {self.player} "
                f"('{self.multiworld.get_player_name(self.player)}'): the selected "
                f"settings require {item_count} item(s) but only {loc_count} check "
                f"location(s) exist — {over} too many to place.{hint}"
            )

        # Ensure the token pool can actually satisfy whatever token gates are
        # active: if the player set a pool lower than a gate they need, force the
        # pool UP to the highest required gate (otherwise the goal/area would be
        # unreachable). Gates depend on mode and goal.
        required_tokens = 0
        if self._is_open_mode():
            goal = options.goal_conditions.value
            # Goals 0,3,4,6 involve Pizza Planet Tokens for the final showdown.
            if goal in (0, 3, 4, 6):
                required_tokens = max(required_tokens,
                                      options.final_showdown_token_gate.value)
        else:
            # Linear mode gates each area behind a token count.
            required_tokens = max(
                options.bombs_away_token_gate.value,
                options.slime_time_token_gate.value,
                options.toy_barn_encounter_token_gate.value,
                options.evil_emperor_zurg_token_gate.value,
                options.linear_final_showdown_token_gate.value,
            )
        token_count_requested = max(token_count_requested, required_tokens)

        # If the gates REQUIRE more tokens than can possibly fit (leaving room for
        # at least one filler item), the seed would be unwinnable — the player
        # could never reach the token gate. Fail generation with a clear, helpful
        # message instead of silently producing a stuck seed. This is the
        # "linear + few/no sanities + default gates" trap: too few check locations
        # to hold the tokens the gates demand.
        if required_tokens > free_slots - 1:
            raise OptionError(
                f"[Toy Story 2] Player {self.player} "
                f"('{self.multiworld.get_player_name(self.player)}'): the "
                f"selected token gates require {required_tokens} Pizza Planet "
                f"Tokens, but only {max(0, free_slots - 1)} check location(s) are "
                f"available to hold them (need 1 spare for filler). Enable more "
                f"sanities (coinsanity/lifesanity/batterysanity/etc.) to add check "
                f"locations, lower your token gates, or both."
            )

        # Place as many Pizza Planet Tokens as requested, but never more than the
        # free slots remaining (so high token requests with few-location settings
        # don't overflow). Tokens take priority over filler.
        token_count = max(0, min(token_count_requested, free_slots - 1))
        if token_count < token_count_requested:
            logging.warning(
                f"[Toy Story 2] Player {self.player}: requested "
                f"{token_count_requested} Pizza Planet Tokens but only "
                f"{token_count} fit the available locations; capped to fit."
            )
        # Only the tokens the goal/gates actually REQUIRE need to be
        # progression (reachable-placed). Marking the surplus as `useful`
        # keeps them out of the restrictive progression fill, so movement
        # unlocks (Double Jump, Ledge Grab, lasers, etc.) aren't crowded out
        # of the few early locations and stranded — which otherwise causes a
        # FillError under aggressive token pools + movesanity. This mirrors
        # the coin-bundle progression-vs-useful split above.
        prog_tokens = min(required_tokens, token_count)
        for i in range(token_count):
            cls = (ItemClassification.progression if i < prog_tokens
                   else ItemClassification.useful)
            items_to_add.append(self._make_item("Pizza Planet Token", cls))

        # Fill any remaining slots with filler/traps.
        item_count = len(items_to_add)
        filler_needed = max(0, loc_count - item_count)

        # Local Filler: rather than constraining the shared pool via local_items
        # (which overflows when this slot has far more checks than its co-op
        # partners — its own progression crowds its world and leaves fewer open
        # spots than it has filler, which is then forbidden from leaving), we
        # RESERVE the spots up front. Dynamic filler items (not traps) are locked
        # into this slot's own open locations before the main fill, so progression
        # fill works around them and item/location counts stay exactly balanced.
        # Traps always travel to the multiworld normally.
        local_filler_on = False
        try:
            local_filler_on = bool(self.options.local_filler)
        except Exception:
            local_filler_on = False

        pending_local: List[ToyStory2Item] = []
        for _ in range(filler_needed):
            name = self._get_filler_or_trap()
            if local_filler_on and name in FILLER_ITEMS:
                pending_local.append(self._make_item(name))
            else:
                items_to_add.append(self._make_item(name))

        if pending_local:
            try:
                own_open = [l for l in self.multiworld.get_locations(self.player)
                            if not l.is_event and l.item is None and not l.locked]
                self.multiworld.random.shuffle(own_open)
                idx = 0
                for it in pending_local:
                    if idx < len(own_open):
                        own_open[idx].place_locked_item(it)
                        idx += 1
                    else:
                        items_to_add.append(it)  # ran out of own spots: pool it
            except Exception:
                # Any failure: fall back to normal (non-local) filler so that
                # generation always succeeds.
                items_to_add.extend(pending_local)

        # Submit all items
        self.multiworld.itempool += items_to_add

    # ── SET RULES ─────────────────────────────────────────────

    def set_rules(self) -> None:
        set_rules(self)

        # Apply level access rules to region entrances
        for level_name in ALL_LEVELS:
            entrance = self.multiworld.get_entrance(f"To {level_name}", self.player)
            entrance.access_rule = lambda state, lv=level_name: \
                can_access_level(state, self.player, lv, self)

    # ── FILLER ────────────────────────────────────────────────

    def get_filler_item_name(self) -> str:
        return self._get_filler_or_trap()

    # ── SLOT DATA ─────────────────────────────────────────────

    def fill_slot_data(self) -> dict:
        options = self.options
        return {
            # Game
            "game_mode":                        options.game_mode.value,
            "skips":                            options.skips.value,
            # Pizza Planet Tokens
            "pizza_planet_token_pool":          options.pizza_planet_token_pool.value,
            # Open Mode
            "starting_levels":                  getattr(self, "_starting_levels", []),
            "omit_airport_infiltration":        options.omit_airport_infiltration.value,
            "omit_elevator_hop":                options.omit_elevator_hop.value,
            "goal_conditions":                  options.goal_conditions.value,
            "final_showdown_token_gate":        options.final_showdown_token_gate.value,
            "defeated_bosses_required":         options.defeated_bosses_required.value,
            # Linear Mode
            "bombs_away_token_gate":            options.bombs_away_token_gate.value,
            "slime_time_token_gate":            options.slime_time_token_gate.value,
            "toy_barn_encounter_token_gate":    options.toy_barn_encounter_token_gate.value,
            "evil_emperor_zurg_token_gate":     options.evil_emperor_zurg_token_gate.value,
            "linear_final_showdown_token_gate": options.linear_final_showdown_token_gate.value,
            # Sanity
            "movesanity":                       options.movesanity.value,
            "coinsanity":                       options.coinsanity.value,
            "coinsanity_checks_bundle_size":    options.coinsanity_checks_bundle_size.value,
            "coinsanity_received_bundle_size":  options.coinsanity_received_bundle_size.value,
            "missing_toy_bundle_size":          options.missing_toy_bundle_size.value,
            "lifesanity":                       options.lifesanity.value,
            "batterysanity":                    options.batterysanity.value,
            "green_laser_sanity":               options.green_laser_sanity.value,
            "rexsanity":                        options.rexsanity.value,
            "hint_block_sanity":                options.hint_block_sanity.value,
            # QOL
            "skip_cutscenes":                   options.skip_cutscenes.value,
            "disc_launcher_fill_pockets":       options.disc_launcher_fill_pockets.value,
            "on_screen_item_feed":              options.on_screen_item_feed.value,
            "disable_falling_animation":        options.disable_falling_animation.value,
            "auto_save":                        options.auto_save.value,
            "start_every_level_with_full_health": options.start_every_level_with_full_health.value,
            "never_game_over":                  options.never_game_over.value,
            # Music
            "music_randomizer_mode":            options.music_randomizer_mode.value,
            "oops_all_bangers_song":            options.oops_all_bangers_song.value,
            "skip_song":                        options.skip_song.value,
            # Death Link
            "death_link":                       options.death_link.value,
            # Traps
            "filler_replaced_with_traps":       options.filler_replaced_with_traps.value,
            "cutscene_trap_weight":             options.cutscene_trap_weight.value,
            "narrow_vision_trap_weight":        options.narrow_vision_trap_weight.value,
            "damage_buzz_trap_weight":          options.damage_buzz_trap_weight.value,
            "freeze_buzz_trap_weight":          options.freeze_buzz_trap_weight.value,
            "invincible_enemies_trap_weight":   options.invincible_enemies_trap_weight.value,
        }

    # ── UNIVERSAL TRACKER SUPPORT ─────────────────────────────
    @staticmethod
    def interpret_slot_data(slot_data: dict) -> dict:
        """Universal Tracker hook. Returning the slot_data (truthy) tells UT to
        re-generate this world and hands the data back via re_gen_passthrough, so
        generate_early can restore the ACTUAL starting levels / options from the
        real seed instead of re-rolling them. Without this, UT re-rolls random
        starting levels and mis-reports which levels are accessible."""
        return slot_data


# Register the BizHawk client handler. This import must happen so the
# ToyStory2Client subclass of BizHawkClient is loaded and discoverable.
from . import ts2_client  # noqa: E402, F401