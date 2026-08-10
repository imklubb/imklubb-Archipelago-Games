"""
TazClient.py -- the Archipelago socket layer for Taz Wanted (PS2).

Built on the same shape as the Sly 2 client, because Taz runs on PCSX2 too and
there is no BizHawk connector to lean on: a CommonContext subclass, a command
processor, and an async task that polls the emulator alongside the server loop.

WHERE THE WORK HAPPENS

  taz_game    every memory read and write
  taz_client  what has been sent, what is owned, what it all means
  this file   the socket, the commands, and the loop that drives the other two

Keeping the socket out of the logic means the logic can be tested without a
server, which is how every rule in taz_client was checked.

RELOADING

  The context never trusts the save file. On connect, and again whenever the
  slot changes, the received-items list is replayed in full and the world is
  rebuilt from it. A save state, a fresh file, or a reconnect mid-session all
  land in the same place.
"""

from typing import Optional
from collections import deque
from time import time
import asyncio
import multiprocessing
import traceback

from CommonClient import get_base_parser, logger, server_loop, gui_enabled
import Utils

# Universal Tracker, when it is installed. Falling back keeps the client
# working for players who do not have it.
tracker_loaded: bool = False
try:
    from worlds.tracker.TrackerClient import (
        TrackerCommandProcessor as ClientCommandProcessor,
        TrackerGameContext as CommonContext,
    )
    tracker_loaded = True
except ImportError:
    from CommonClient import ClientCommandProcessor, CommonContext

from . import logic as D
from . import map_view as MV
from . import client as L
from . import game as _game

GAME_NAME = "Taz Wanted"

# What the rest of the multiworld is told. The message used to say "got
# caught" whatever had happened, which is misleading when Taz drowned.
DEATH_TEXT = {
    "captures":  "Taz got caught!",
    "drown":     "Why for you drown Taz?",
    "void_out":  "Taz didn't survive the fall...",
    "boss loss": "Taz lost the good fight...",
}

# The tracker's tab for everywhere that is not a level.
TRACKER_HOME = "Main Map"
CLIENT_VERSION = [1, 0, 0]

DIFFICULTY_WARNING = (
    "WARNING! You forgot to set your difficulty to the setting matching your "
    "yaml. Please return to the title screen and start a new file with the "
    "proper difficulty in place. Otherwise some of your checks may never send."
)


class TazCommandProcessor(ClientCommandProcessor):  # type: ignore[misc]
    def _cmd_deathlink(self):
        """Toggle DeathLink, overriding the yaml setting."""
        if isinstance(self.ctx, TazContext):
            ctx = self.ctx
            ctx.death_link_enabled = not ctx.death_link_enabled
            ctx.logic.opt["death_link"] = ctx.death_link_enabled
            Utils.async_start(
                ctx.update_death_link(ctx.death_link_enabled),
                name="Update Deathlink")
            logger.info(f"DeathLink "
                        f"{'enabled' if ctx.death_link_enabled else 'disabled'}")

    def _cmd_taz(self):
        """Where the run currently stands."""
        if not isinstance(self.ctx, TazContext):
            return
        ctx = self.ctx
        lg = ctx.logic
        logger.info(f"  mode          {lg.mode}")
        logger.info(f"  locations     {len(lg.sent)} of {len(lg.locations)} "
                    f"sent")
        if lg.mode == "open":
            logger.info(f"  levels        {len(lg.levels)} of "
                        f"{len(D.LEVELS)} unlocked")
            logger.info(f"  bosses        {len(lg.bosses)} unlocked")
        logger.info(f"  posters       {lg.posters}")
        logger.info(f"  costumes      {len(lg.costumes)}")
        logger.info(f"  bonus games   {len(lg.bonus)}")
        p, b, u = lg.goal_remaining()
        parts = []
        if p:
            parts.append(f"{p} poster(s)")
        if b:
            parts.append(f"{b} boss(es)")
        if u:
            parts.append("the Hindenbird unlock")
        logger.info("  goal          "
                    + ("met!" if not parts else "needs " + ", ".join(parts)))

    def _cmd_goal(self):
        """What this seed asks for, and how far along it is.

        Two different questions depending on the mode, so two different
        answers. Open has a goal made of up to three conditions and no gates;
        Linear has no goal conditions to choose from -- the whole run is the
        poster gates -- so listing those IS the answer to "what do I need".

        Everything is counted the way the client actually gates on it:
        RECEIVED Wanted Posters and Hindenbird Tickets, never what has been
        smashed or beaten in game. Those are different numbers, and a status
        line that showed the wrong one would be worse than no line at all.
        """
        if not isinstance(self.ctx, TazContext):
            return
        lg = self.ctx.logic
        if lg is None:
            logger.info("  Not connected to a seed yet.")
            return
        o = lg.opt

        if lg.mode == "linear":
            open_bosses = lg.linear_open_bosses()
            logger.info(f"  Linear -- the poster gates are the run. "
                        f"{lg.posters} Wanted Poster(s) received.")
            for name, key, boss in (
                    ("Elephant Pong", "gate_elephant_pong", 7),
                    ("Gladiatoons", "gate_gladiatoons", 12),
                    ("Dodge City", "gate_dodge_city", 17),
                    ("Disco Volcano & Hindenbird", "gate_disco_volcano", 19)):
                need = int(o.get(key, 0))
                state = ("OPEN" if boss in open_bosses
                         else f"{max(0, need - lg.posters)} more to go")
                logger.info(f"    {name:<28}{need:>4} posters   {state}")
            logger.info(f"  in the seed   {int(o.get('poster_pool', 0))} "
                        f"Wanted Posters exist to find")
            logger.info("  then          beat Tweety on The Hindenbird")
            return

        # Open. Only the conditions the player's Goal Conditions actually
        # picked: listing the other two would read as things they still have
        # to do.
        p, b, u = lg.goal_remaining()
        logger.info("  Open -- The Hindenbird opens once the goal is met.")
        shown = False
        if o["posters_in_goal"]:
            shown = True
            need = int(o.get("goal_posters", 0))
            left = "done" if not p else f"{p} to go"
            logger.info(f"    Wanted Posters          {lg.posters:>4} of "
                        f"{need:<5}{left:<10}"
                        f"({int(o.get('poster_pool', 0))} in the seed)")
        if o["bosses_in_goal"]:
            shown = True
            need = int(o.get("goal_bosses", 0))
            left = "done" if not b else f"{b} to go"
            logger.info(f"    Hindenbird Tickets      {lg.tickets:>4} of "
                        f"{need:<5}{left:<10}(one per boss defeated)")
        if o["unlock_in_goal"]:
            shown = True
            logger.info("    The Hindenbird Unlock   "
                        + ("     not received yet" if u else "     received"))
        if not shown:
            logger.info("    The Hindenbird Unlock -- nothing else selected")
        logger.info("  requirements  "
                    + ("met, the fight is open" if not (p or b or u)
                       else "not met yet"))
        logger.info("  then          beat Tweety on The Hindenbird")

    def _cmd_difficulty(self):
        """What the game is set to, and what the yaml expects."""
        if not isinstance(self.ctx, TazContext):
            return
        want = self.ctx.logic.opt["difficulty"]
        have = self.ctx.logic.game.difficulty()
        logger.info(f"  yaml: {want}   game: {have or 'unknown'}")
        if have and have != want:
            logger.warning(DIFFICULTY_WARNING)

    def _cmd_resync(self):
        """Rebuild everything from the server's item list.

        Rarely needed -- it happens automatically on connect -- but it is a
        one-command fix if the world ever looks out of step.
        """
        if isinstance(self.ctx, TazContext):
            self.ctx.force_resync = True
            logger.info("Resyncing from the server on the next tick.")


class TazContext(CommonContext):  # type: ignore[misc]
    """The client. The map view is attached here rather than owned by it, so
    a Kivy that will not cooperate costs the map tab and nothing else."""

    command_processor = TazCommandProcessor
    game = GAME_NAME
    items_handling = 0b111          # full remote items

    # Connect as the real game slot rather than a read-only tracker. UT's
    # context adds a "Tracker" tag that has to come off as a class attribute:
    # removing it later leaves a window where the connection is already made
    # with the wrong tags.
    if tracker_loaded:
        tags = CommonContext.tags - {"Tracker"}

    sync_task: Optional[asyncio.Task] = None
    is_connected_to_game: bool = False
    is_connected_to_server: bool = False
    slot_data: Optional[dict] = None
    last_error_message: Optional[str] = None

    # Where the tracker looks to know which tab to show. The client writes it,
    # the pack Gets and SetNotifies it -- the same bridge Toy Story 2 uses.
    map_tab = None
    level_key: Optional[str] = None
    last_level_sent: Optional[str] = None

    death_link_enabled: bool = False
    deaths_to_apply: int = 0
    last_death_sent: float = 0.0
    force_resync: bool = False
    synced_once: bool = False

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.version = CLIENT_VERSION
        # Built with defaults now and rebuilt from slot data on Connected, so
        # the emulator can be polled -- and reported on -- before the player
        # has joined a room.
        self.logic: Optional[L.Client] = L.Client({}, seed="pending")
        self.notification_queue = deque(maxlen=200)
        self._known_items = 0
        self._warned_difficulty = False

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def make_gui(self):
        """Name the window after this game rather than the tracker.

        The same shape Toy Story 2 uses: wrap whatever manager the base
        context provides and override its title, so nothing about the
        interface is rebuilt.
        """
        ui = super().make_gui()

        class TazManager(ui):           # type: ignore[misc,valid-type]
            base_title = "Archipelago Taz Wanted Client"

        return TazManager

    def on_package(self, cmd: str, args: dict):
        # Universal Tracker's on_package does NOT chain upward, so overriding
        # it here without calling super means the tracker never sees Connected
        # and never learns the slot exists. Its tab then renders with nothing
        # but its own startup line -- which is exactly what happened.
        try:
            super().on_package(cmd, args)
        except Exception:
            logger.exception("the tracker's packet handler raised")

        if cmd == "Connected":
            self.slot_data = args.get("slot_data", {}) or {}
            self._build_logic()
            self.force_resync = True
            # _build_logic makes a NEW client, whose received-items list is
            # empty -- so the next sync sees every item the server has ever
            # sent as brand new. Without this a reconnect re-fired every
            # filler and every trap the player had ever been given, all at
            # once, and replayed every notification with them.
            #
            # synced_once is about "has THIS client object been filled in",
            # not "has this session ever connected", and only the connect
            # that rebuilds it can know that.
            self.synced_once = False
            logger.info(f"Connected. {len(self.logic.locations)} location(s) "
                        f"in this seed.")
            self.level_key = f"taz_current_level_{self.slot}"
            # Scout the three checks the game announces with its own message
            # boxes, so those boxes can say what was actually sent instead of
            # promising a bonus game the seed may have replaced.
            #
            # create_as_hint is 0: this is for wording a message the player is
            # about to see anyway, not for spending their hints.
            ids = self.logic.bonus_message_locations()
            if ids:
                Utils.async_start(self.send_msgs([{
                    "cmd": "LocationScouts", "locations": ids,
                    "create_as_hint": 0}]), name="Taz bonus scouts")
            if self.logic.opt["death_link"]:
                self.death_link_enabled = True
                Utils.async_start(self.update_death_link(True),
                                  name="Update Deathlink")

        elif cmd == "LocationInfo":
            for net in args.get("locations", []) or []:
                try:
                    item = self.item_names.lookup_in_slot(
                        net.item, net.player)
                except Exception:
                    item = None
                who = self.player_names.get(net.player)
                if item and who:
                    self.logic.bonus_scouts[int(net.location)] = (item, who)

        elif cmd == "ReceivedItems":
            self.force_resync = True

        elif cmd == "RoomInfo":
            self.seed_name = args.get("seed_name", "")

    def on_deathlink(self, data: dict):
        """Someone else died, so Taz goes back to the start of the level."""
        super().on_deathlink(data)
        self.deaths_to_apply += 1

    def _build_logic(self):
        """Create the logic layer from the slot data.

        The seed name is used to key the sent-locations record, so two seeds
        played from the same folder do not contaminate each other.
        """
        opts = self.slot_data or {}
        seed = getattr(self, "seed_name", "") or "local"
        self.logic = L.Client(opts, seed=f"{seed}_{self.slot}")
        self.logic.game.connect()

    def received_item_details(self):
        """(name, sender or None, classification flags) for every item.

        Kept index-aligned with received_item_names -- both are built from
        this one loop, so an item the DataPackage cannot name is dropped
        from both and the two lists can never drift apart.
        """
        out = []
        for net in self.items_received:
            name = None
            try:
                name = self.item_names.lookup_in_game(net.item, GAME_NAME)
            except Exception:
                try:
                    name = self.item_names.lookup_in_slot(net.item)
                except Exception:
                    pass
            if not name:
                continue
            who = getattr(net, 'player', None)
            own = who is None or who == self.slot
            sender = None if own else self.player_names.get(who)
            flags = int(getattr(net, 'flags', 0) or 0)
            out.append((name, sender, flags))
        return out

    def received_item_names(self):
        """Every item the server has given us, as names, in order.

        Not called `item_names`: CommonContext already has an attribute by that
        name holding the DataPackage lookup, and shadowing it would make this
        call itself.

        Derived from received_item_details so the two cannot drift apart.
        """
        return [d[0] for d in self.received_item_details()]


def _say_red(ctx: TazContext, text: str):
    """Put a line in the client log in red.

    The log escapes markup in ordinary logger calls, so Kivy colour tags in
    the message do nothing. What the client DOES colour is print_json, which
    is how the server's own messages get their colours -- so the warning goes
    through the same path, one coloured part per line.

    Falls back to the logger if that path is unavailable, because a warning
    that does not appear at all is far worse than one that is not red.
    """
    try:
        parts = []
        for i, chunk in enumerate(text.split("\n")):
            if i:
                parts.append({"text": "\n"})
            parts.append({"type": "color", "color": "red", "text": chunk})
        ctx.on_print_json({"data": parts})
        return
    except Exception:
        pass
    logger.error(text)


def update_connection_status(ctx: TazContext, status: bool):
    if ctx.is_connected_to_game == status:
        return
    if status:
        logger.info("Connected to Taz Wanted")
    else:
        logger.info("Unable to reach PCSX2, retrying. Check that the game is "
                    "running and that PINE is enabled in "
                    "Settings > Advanced.")
    ctx.is_connected_to_game = status


async def pcsx2_sync_task(ctx: TazContext):
    # The GUI is built on another thread, so the map is attached from here --
    # the first tick that finds an interface gets it.
    logger.info("Starting the Taz connector, looking for PCSX2...")

    # Without pine there is nothing to connect to, and the loop below would
    # spin forever saying nothing. Say it once, clearly, instead.
    if _game.mem is None:
        logger.error(
            "PINE is unavailable, so the client cannot reach PCSX2. "
            "pine.py belongs at worlds/tazwanted/pcsx2_interface/pine.py -- "
            "if it is there, the error from importing it is above.")
        return

    while not ctx.exit_event.is_set():
        try:
            if gui_enabled:
                _attach_map(ctx)

            if ctx.logic is None:
                await asyncio.sleep(1)
                continue
            connected = ctx.logic.game.alive()
            update_connection_status(ctx, connected)
            if connected:
                await _handle_ready(ctx)
            else:
                await _handle_not_ready(ctx)
        except ConnectionError as e:
            ctx.is_connected_to_game = False
            if "timed out" in str(e).lower():
                # PINE allows one connection per slot, so another tool holding
                # it looks exactly like the emulator being unresponsive.
                logger.warning(
                    "PINE timed out. Another tool may be connected on the "
                    "same slot -- close any other PCSX2 scripts and retry.")
            await asyncio.sleep(1)
        except Exception as e:
            if isinstance(e, RuntimeError):
                logger.error(str(e))
            else:
                logger.error(traceback.format_exc())
            await asyncio.sleep(3)


def _attach_map(ctx: TazContext) -> None:
    """Add the maps as a client tab, once the interface exists.

    add_client_tab is the client's own API for this. Driving the tab bar
    directly threw, because it is an MDNavigationBar and expects nothing of
    the sort.

    run_gui() builds the interface on another thread, so this is called from
    the poll loop and simply waits until ctx.ui is there.
    """
    if ctx.map_tab is not None or getattr(ctx, "_map_failed", False):
        return
    ui = getattr(ctx, "ui", None)
    if ui is None or not hasattr(ui, "add_client_tab"):
        ctx._map_tries = getattr(ctx, "_map_tries", 0) + 1
        if ctx._map_tries > 300:
            ctx._map_failed = True
            logger.info("No map tab: this client has no add_client_tab.")
        return
    try:
        import os
        data = MV.load_maps()
        if not data or not MV.KIVY:
            ctx._map_failed = True
            return
        # Unpacked to real files first: Kivy cannot read a texture out of the
        # .apworld zip, which is why the pins appeared over a blank panel.
        from . import _imports as _imp
        here = _imp.extract_images([m["image"] for m in data["maps"]])
        tab = MV.MapTab(data)
        root = tab.build(here)
        if root is None:
            ctx._map_failed = True
            return
        ui.add_client_tab("Map", root)
        ctx.map_tab = tab
        logger.info(f"Map view ready: {len(data['maps'])} maps.")
    except Exception as exc:
        ctx._map_failed = True
        logger.info(f"No map tab this session: {exc!r}")


async def _handle_ready(ctx: TazContext) -> None:
    lg = ctx.logic
    game = lg.game

    if ctx.server is None or ctx.slot is None:
        msg = "Waiting for the player to connect to a server"
        if ctx.last_error_message != msg:
            logger.info(msg)
            ctx.last_error_message = msg
        await asyncio.sleep(1)
        return
    ctx.last_error_message = None

    # No early return for "no save file". The title screen is where the
    # difficulty warning matters most and where some of the text is seen, and
    # tick() decides for itself which work is safe where.

    # Replay the whole item list rather than tracking what is new. It costs
    # almost nothing and means a reconnect, a save state, or a different save
    # file all rebuild to the same place.
    if ctx.force_resync:
        ctx.force_resync = False
        # Silent: a resync happens on every ReceivedItems, which means after
        # every check the player sends. /taz reports the same thing on demand.
        # Only the FIRST sync is a replay. This one call site handles every
        # item delivery, so marking them all as replays meant no filler or
        # trap ever fired again -- the reconnect fix broke the ordinary case.
        first = not ctx.synced_once
        ctx.synced_once = True
        details = ctx.received_item_details()
        lg.receive([d[0] for d in details], replay=first, details=details)

    for line in lg.tick():
        if line.startswith(L.ERROR_PREFIX):
            _say_red(ctx, line[len(L.ERROR_PREFIX):])
        else:
            logger.info(line)


    new = sorted(lg.sent - set(ctx.checked_locations))
    if new:
        await ctx.send_msgs([{"cmd": "LocationChecks", "locations": new}])

    # One line per state change, so the next report says which of the three
    # possibilities it is: the death never registers, it registers and is
    # refused, or it registers and the send is lost.
    if lg.deaths_pending and not ctx.death_link_enabled:
        # Approved by the logic but refused here, which is exactly the silence
        # being chased: death_link_enabled is read from slot data once, and a
        # yaml with DeathLink off leaves it false however the sources are set.
        lg.deaths_pending = 0
        logger.warning("A death was detected but DeathLink is off for this "
                       "slot -- set death_link in the yaml, or use /deathlink.")

    if lg.deaths_pending and ctx.death_link_enabled:
        # The cooldown decides BEFORE the counter is cleared. Clearing first
        # meant a death within three seconds of the last was dropped rather
        # than delayed -- and the player saw nothing either way.
        if time() - ctx.last_death_sent > 3:
            who = ctx.player_names.get(ctx.slot, "Taz")
            text = DEATH_TEXT.get(lg.last_death_kind,
                                  f"{who} died.").format(who=who)
            try:
                # No local line: Archipelago prints one of its own, and two
                # per death reads like it fired twice. The text below is what
                # the rest of the multiworld sees.
                await ctx.send_death(text)
            except Exception as exc:
                # The counter is only cleared once the send has actually
                # happened. Clearing it first meant a failed send threw the
                # death away in silence.
                logger.error(f"DeathLink send failed: {exc!r}")
            else:
                ctx.last_death_sent = time()
                lg.deaths_pending = 0

    # Held rather than dropped when Taz cannot take one right now -- as the
    # ball or the mouse, where being teleported to the level start is a soft
    # lock. The counter is only decremented once the death is actually acted
    # on, so nothing is lost while he is transformed.
    while ctx.deaths_to_apply > 0 and lg.can_apply_death():
        ctx.deaths_to_apply -= 1
        lg.apply_death()

    # Tell the tracker where Taz is, so it can follow him between tabs. Only
    # on a change: this is a network write, not a poll.
    #
    # Anywhere that is not one of the ten levels -- a hub, a boss arena, the
    # title screen -- resolves to the main map. Sending a hub's own name meant
    # asking for a tab that does not exist, so the tracker simply stayed on
    # whichever level the player had last been in.
    if ctx.level_key:
        lid = game.level_id()
        where = D.LEVEL_NAME.get(lid) or TRACKER_HOME
        if where != ctx.last_level_sent:
            ctx.last_level_sent = where
            if ctx.map_tab:
                try:
                    ctx.map_tab.show(where)
                except Exception:
                    pass
            await ctx.send_msgs([{
                "cmd": "Set", "key": ctx.level_key,
                "default": "", "want_reply": False,
                "operations": [{"operation": "replace", "value": where}],
            }])

    _map_state = (len(lg.sent), len(lg.received))
    if ctx.map_tab and _map_state != getattr(ctx, "_map_sent", None):
        ctx._map_sent = _map_state
        try:
            names = {lg.by_id[i]["name"] for i in lg.sent if i in lg.by_id}
            # What the player owns decides what is green, so it travels with
            # the settings rather than being read separately.
            opt = dict(lg.opt)
            opt["_owned_items"] = list(lg.received)
            # Linear gates on posters rather than unlocks, so the count has to
            # travel with the settings.
            opt["_posters"] = lg.posters
            ctx.map_tab.refresh(names, opt)
        except Exception:
            pass

    if lg.goal_met() and not ctx.finished_game:
        await ctx.send_msgs([{"cmd": "StatusUpdate", "status": 30}])
        ctx.finished_game = True
        logger.info("Goal complete!")

    await asyncio.sleep(0.1)


async def _handle_not_ready(ctx: TazContext):
    if not ctx.exit_event.is_set() and ctx.logic is not None:
        ctx.logic.game.connect()
    await asyncio.sleep(3)


def launch_client():
    Utils.init_logging("Taz Client")

    async def main(args):
        multiprocessing.freeze_support()
        ctx = TazContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx),
                                              name="Server Loop")

        # Build the tracker's simulated world. A failure here must not take
        # the client down -- but it must be VISIBLE, because a silent failure
        # is indistinguishable from the tracker simply not being installed,
        # and leaves an empty tab with nothing to explain it.
        if tracker_loaded:
            try:
                ctx.run_generator()
            except Exception as exc:
                logger.exception(exc)
                logger.error(
                    "Universal Tracker generation failed. The client still "
                    "works; the Tracker tab will be empty. The traceback "
                    "above says why.")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        # Started before the wait, and before anything blocks: the emulator
        # poll has to run whether or not a server connection exists yet.
        ctx.sync_task = asyncio.create_task(pcsx2_sync_task(ctx),
                                            name="PCSX2 Sync")
        await ctx.exit_event.wait()
        ctx.server_address = None
        if ctx.sync_task:
            await asyncio.wait([ctx.sync_task], timeout=5)

    parser = get_base_parser()
    args, _ = parser.parse_known_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    launch_client()
