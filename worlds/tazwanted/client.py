#!/usr/bin/env python3
"""
taz_client.py -- the Archipelago client for Taz Wanted (PS2).

Sits above taz_game, which owns the memory. This owns the connection, what has
been sent, what has been received, and what all of it means.

THE CLIENT IS THE SOURCE OF TRUTH

  Nothing here infers progress from the save file. What the player owns comes
  from the server's received-items list; what has been sent is recorded here.
  On connect -- or after a save state, or a reload, or a switch to a different
  file -- every granted item is applied again from scratch.

  That is what makes reloading safe. A save state from before an item arrived
  cannot lose it, and a save file that already shows a level complete cannot
  send a check that was never earned.

SENDING ONCE

  A location is sent when it first appears satisfied and never again. The
  record lives with the seed, so quitting and coming back does not resend
  everything the save file happens to show.

WHAT RUNS EACH TICK

  read the world, evaluate locations, send anything new, apply every granted
  item, run the sandwich spoof, watch for deaths, and keep the on-screen text
  in step with what the player still needs.
"""

import argparse
import json
import os
import sys
import time
from collections import deque

from . import _imports
from . import logic as D
from . import logic as O
from . import game as G
from . import health as H
from . import notify as N

# The layout and the text helpers live in game.py too. They need pine, which a
# generation server does not have, so every use is guarded.
T = G
S = G

TICK = 0.1
# Data that ships inside the world is read through _imports.data, which works
# whether the world is a folder or a .apworld zip. Paths built from __file__
# point inside the archive in the zip case, so open() fails and every file
# silently goes missing.
SPAWN_FILE = "taz_spawns.json"
CATCHER_FILE = "taz_catchers.json"

# The record of what has been sent belongs with the player's own files rather
# than inside the world, so this one stays relative to where the client runs.
# One per seed, in a folder of its own: they used to sit loose in the
# Archipelago root next to the launcher, and they accumulate.
#
# These are NOT logs. Each one holds which locations have been sent, which
# catchers are banked and the void-out count -- delete one and that seed
# re-derives from scratch, which re-sends checks and loses the void tally.
STATE_FILE = "taz_client_state.json"
STATE_DIR = "taz_wanted_states"

# Written by taz_pad.py once the shoulder-button address has been measured.
# Absent, the in-game hotkey is simply inactive and everything else works.
PAD_FILE = "taz_pad.json"

# Item name -> what it grants.
LEVEL_UNLOCK_OF = {f"{name} Unlock": lid for lid, name in D.LEVELS}
BOSS_UNLOCK_OF = {
    "Elephant Pong Unlock": 7, "Gladiatoons Unlock": 12,
    "Dodge City Unlock": 17, "Disco Volcano Unlock": 19,
    "The Hindenbird Unlock": 20,
}
BONUS_UNLOCK_OF = {f"{name} Bonus Game Unlock": lid for lid, name in D.LEVELS
                   if lid not in D.NO_BONUS}

WANTED_POSTER = "Wanted Poster"
HINDENBIRD_TICKET = "Hindenbird Ticket"

# Shown on a boss door that has been granted while its hub was already loaded.
# 38 characters -- Dodge City's vanilla line is the shortest slot at 42, and
# the table is packed, so anything longer eats the next entry.
BOSS_RELOAD_TEXT = "Reload the hub to challenge this boss!"

TAZLAND = 18

# How long to distrust everything after the attract-mode demo stops. Generous
# on purpose: a check sent by the demo cannot be taken back.
DEMO_SETTLE = 10.0

EFFECT_ITEMS = {
    "Raised Bounty": "bounty", "Chili Pepper": "pepper",
    "Burp Can": "burp", "Invisibility": "invisibility",
    "Bubble Gum": "bubblegum",
    "Dynamite Trap": "dynamite", "Electrocute Trap": "electrocute",
    "Squash Trap": "squash",
    "Hiccup Trap": "hiccup", "No Spinning Trap": "no_spin",
    "Costume Strip Trap": "lose_costume",
}

# Messages starting with this are logged as errors, which the client GUI
# renders in red.
ERROR_PREFIX = "!!"

DIFFICULTY_WARNING = (
    "WARNING! You forgot to set your difficulty to the setting matching your "
    "yaml.\n  Please return to the title screen and start a new file with the "
    "proper difficulty in place.\n  Otherwise some of your checks may never "
    "send.")


def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


class Client:
    def __init__(self, options, seed="local"):
        self.opt = O.normalise(options)
        self.seed = seed
        self.mode = self.opt["mode"]
        self.game = G.Game()

        cat = _imports.data(CATCHER_FILE) or {}
        self.catcher_table = cat
        self.locations = D.all_locations(catchers=cat,
                                         **O.location_args(self.opt))
        self.by_id = {l["id"]: l for l in self.locations}
        self.spawns = _imports.data(SPAWN_FILE) or {}

        # What the server has given us, and what we have told it about.
        self.received = []              # item names, in order
        self.sent = set()               # location ids
        self.catcher_kills = set()      # (level, index)
        self.completed = set()          # level ids seen complete

        # location id -> (item name, receiving player), for the three checks
        # the game announces with its own message boxes. Filled by the client
        # from Archipelago's LocationScouts; empty until those come back, and
        # bonus_line falls back to wording that says nothing it cannot know.
        self.bonus_scouts = {}

        # Derived from `received`; rebuilt from scratch whenever it changes, so
        # a reload can never leave it half applied.
        self.levels = set()
        self.bosses = set()
        self.boss_needs_reload = set()  # granted, but its hub predates it
        self.bonus = set()
        self.costumes = set()
        self.posters = 0
        self.tickets = 0

        self.effect_queue = deque()
        self.active_traps = {}
        self._effect_after = 0.0
        self.boss_loss_until = 0.0
        self._demo_until = 0.0
        self.last_death_kind = None
        self.last_death_skipped = None
        self.deaths_pending = 0
        self.void_seen = 0
        self._last_level = None
        self._warned_difficulty = False
        self._last_text = None
        self._seeded = False

        self.load_state()

        # In-game notification text. The yaml sets the starting mode; a
        # hotkey change is remembered per seed and wins next time.
        mode = N.MODE_FROM_OPTION.get(
            str(self.opt.get('in_game_text', 'progressive')), N.PROGRESSION)
        if self._saved_text_mode in (N.OFF, N.PROGRESSION, N.ALL):
            mode = self._saved_text_mode
        self.notify = N.Notifier(mode)
        # A flight recorder. Read-only, and it must never be able to stop the
        # client: a diagnostic that takes the session down with it is worse
        # than no diagnostic.
        self.health = H.make(G.mem, self.state_path())
        N.configure_pad(PAD_FILE)

        self.rebuild()
        self.game.start_catchers(cat)
        self.game.load_exits()
        if not self.game.load_gates():
            self._gate_warning = ("no taz_gates.json found -- the pushback "
                                  "zones are inactive")
        else:
            self._gate_warning = None

    # ---------------------------------------------------------------- state

    def state_name(self):
        return f"{STATE_FILE.rsplit('.', 1)[0]}_{self.seed}.json"

    def state_path(self):
        return os.path.join(STATE_DIR, self.state_name())

    def legacy_state_path(self):
        """Where it used to live: loose in the Archipelago root."""
        return self.state_name()

    def migrate_state(self):
        """Move an old root-level state file into the folder, once.

        Moved rather than copied, so there is exactly one file and no
        question later about which of two is authoritative. If the move
        fails -- a permission problem, a file in use -- the old path is
        returned and read in place, because losing a seed's sent-list to
        tidiness would be a bad trade.
        """
        new, old = self.state_path(), self.legacy_state_path()
        if os.path.exists(new) or not os.path.exists(old):
            return new
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            os.replace(old, new)
            return new
        except Exception:
            return old

    def load_state(self):
        d = load_json(self.migrate_state(), {})
        self.sent = set(d.get("sent", []))
        self.catcher_kills = {tuple(x) for x in d.get("catchers", [])}
        self.completed = set(d.get("completed", []))
        self.void_seen = d.get("void_seen", 0)
        # The player's own hotkey changes outrank the yaml from then on.
        self._saved_text_mode = d.get("in_game_text")
        # Which save file this seed was last played on, so the prompt can
        # name it instead of asking for a file the player already chose.
        # Per-seed, because the state file is per-seed.
        self.last_file = d.get("last_file")
        self._file_prompt = None

    def save_state(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
        except Exception:
            pass
        with open(self.state_path(), "w", encoding="utf-8") as f:
            json.dump({"sent": sorted(self.sent),
                       "catchers": sorted(self.catcher_kills),
                       "completed": sorted(self.completed),
                       "void_seen": self.void_seen,
                       "in_game_text": getattr(
                           getattr(self, "notify", None), "mode", None),
                       "last_file": getattr(self, "last_file", None),
                       }, f, indent=2)

    def rebuild(self):
        """Recompute everything owned from the received-items list.

        Deliberately from scratch rather than incrementally: an incremental
        update is only correct if every item was seen exactly once, which a
        reconnect cannot promise.
        """
        self.levels, self.bosses, self.bonus, self.costumes = \
            set(), set(), set(), set()
        self.posters = self.tickets = 0

        # Starting levels are precollected at generation, so the server never
        # sends them as items -- the client would otherwise believe the player
        # owns nothing and geofence them out of every door, including the
        # levels they are supposed to begin with.
        for name in self.opt.get("starting_levels_granted", []):
            if name in LEVEL_UNLOCK_OF:
                self.levels.add(LEVEL_UNLOCK_OF[name])

        for name in self.received:
            if name in LEVEL_UNLOCK_OF:
                self.levels.add(LEVEL_UNLOCK_OF[name])
            elif name in BOSS_UNLOCK_OF:
                self.bosses.add(BOSS_UNLOCK_OF[name])
            elif name in BONUS_UNLOCK_OF:
                self.bonus.add(BONUS_UNLOCK_OF[name])
            elif name in G.COSTUME_BY_NAME:
                self.costumes.add(G.COSTUME_BY_NAME[name])
            elif name == WANTED_POSTER:
                self.posters += 1
            elif name == HINDENBIRD_TICKET:
                self.tickets += 1

    def receive(self, names, replay=False, details=None):
        """Take the server's full item list and re-derive from it.

        `details` is the parallel (name, sender, flags) list, used only for
        the on-screen notifications. It is optional so a caller that does
        not have it still works.
        """
        first = len(self.received)
        new = names[first:]
        self.received = list(names)
        self.rebuild()
        # Filler and traps are moments, not possessions. A reconnect replays
        # the whole item list to rebuild what the player OWNS, and firing
        # every pepper and every trap again along the way is not that.
        if not replay:
            # Which poster this is counts from everything already received,
            # not from this batch, or a reconnect would restart at one.
            poster_n = sum(1 for x in names[:first] if x == WANTED_POSTER)
            for i, n in enumerate(new):
                if n in EFFECT_ITEMS:
                    self.effect_queue.append(EFFECT_ITEMS[n])
                # A reconnect replays the whole list to rebuild what the
                # player owns; announcing all of it again would bury them.
                d = None
                if details is not None and first + i < len(details):
                    d = details[first + i]
                sender = d[1] if d else None
                flags = d[2] if d else N.FLAG_PROGRESSION
                if n == WANTED_POSTER:
                    poster_n += 1
                    self.notify.push(N.poster_line(poster_n, sender),
                                     N.ALWAYS)
                else:
                    self.notify.push(
                        N.item_line(n, sender, own=not sender), flags)
        return new

    # ---------------------------------------------------------------- goal

    def goal_remaining(self):
        o = self.opt
        posters = max(0, int(o.get("goal_posters", 0)) - self.posters) \
            if o["posters_in_goal"] else 0
        bosses = max(0, int(o.get("goal_bosses", 0)) - self.tickets) \
            if o["bosses_in_goal"] else 0
        unlock = o["unlock_in_goal"] and 20 not in self.bosses
        return posters, bosses, unlock

    def hindenbird_requirements_met(self):
        """Is the last fight open?

        Deliberately NOT goal_met(): that also asks whether Tweety has been
        beaten, which is the thing the open door is for.

        In Open the unlock is an item only when the player made it part of
        their goal. When they did not, no such item exists in the seed at all,
        so asking "has the unlock arrived" would keep the fight shut for the
        whole run. What the player chose is what opens it -- which is exactly
        what goal_remaining already works out.
        """
        p, b, u = self.goal_remaining()
        return p == 0 and b == 0 and not u

    def granted_bosses(self):
        """self.bosses, plus the Hindenbird once its requirements are met."""
        out = set(self.bosses)
        if self.mode == "open" and self.hindenbird_requirements_met():
            out.add(20)
        return out

    def goal_met(self):
        """The run is won when Tweety is beaten.

        The requirements are what OPENS the fight; beating it is what wins.
        Checking only the requirements marked the run complete the moment a
        player qualified, which is not what the goal says and would end their
        seed without the final boss.
        """
        p, b, u = self.goal_remaining()
        if not (p == 0 and b == 0 and not u):
            return False
        return self.game.hindenbird_beaten()

    def linear_open_levels(self):
        """Which levels Linear allows, from the poster gates.

        The geofences need this as well as the access fields: Tazland is
        natively open and guarded only by its bridge, so without it the bridge
        blocks forever in Linear.
        """
        open_bosses = self.linear_open_bosses()
        out = {4, 5, 6}
        if 7 in open_bosses:
            out |= {9, 10, 11}
        if 12 in open_bosses:
            out |= {14, 15, 16}
        if 17 in open_bosses:
            out.add(18)
        return out

    def linear_open_bosses(self):
        """Which bosses the poster count has unlocked, in Linear.

        RECEIVED Wanted Posters, not the ones broken in game. They are two
        completely different numbers and this used to ask for the wrong one.

        The generator requires the items -- entrance_rules and goal_rule in
        Linear are `_has(s, "Wanted Poster", n)`, and a Linear pool carries
        one poster per gate as PROGRESSION. Opening the boss on posters the
        player smashed themselves instead means the seed's own logic describes
        a game nobody is playing: the gates come open on their own, and the
        items that were supposed to open them do nothing.

        Breaking a poster is still what SENDS the check. It is just not what
        opens the door.
        """
        o = self.opt
        have = self.posters
        out = set()
        # The Hindenbird shares Disco Volcano's gate: one leads straight into
        # the other, so a separate number would be meaningless.
        for boss, key in ((7, "gate_elephant_pong"), (12, "gate_gladiatoons"),
                          (17, "gate_dodge_city"), (19, "gate_disco_volcano"),
                          (20, "gate_disco_volcano")):
            if have >= int(o.get(key, 0)):
                out.add(boss)
        return out

    # ---------------------------------------------------------------- text

    def update_text(self):
        """Text lives outside the save region, so it is safe on the title
        screen -- which is the only place some of it is seen.

        Errors are reported rather than swallowed. A blanket except here meant
        a wrong offset or a missing name looked identical to the text simply
        not needing an update, which is how three separate text problems went
        unnoticed at once.
        """
        if S is None or not self.game.alive():
            return []
        out = []
        lid = self.game.level_id()

        # Locked names.
        #
        # Open: every slot, since the levels and bosses are all items. The
        # Hindenbird is excluded because its slot carries the goal summary.
        #
        # Linear: the levels label themselves, but the bosses are gated on a
        # poster count the game knows nothing about, so those still need it.
        try:
            if self.mode == "open":
                for slot_id, slot in S.LEVEL_NAMES.items():
                    if slot_id == 20:
                        continue
                    addr, original = slot[0], slot[1]
                    granted = (slot_id in self.levels
                               or slot_id in self.bosses)
                    want = original if granted else S.LOCKED_TEXT
                    if S.read_w(addr) != want:
                        S.write_w(addr, want, len(original))
            else:
                open_bosses = self.linear_open_bosses()
                for boss_id in (7, 12, 17, 19, 20):
                    slot = S.LEVEL_NAMES.get(boss_id)
                    if not slot:
                        continue
                    addr, original = slot[0], slot[1]
                    want = (original if boss_id in open_bosses
                            else S.LOCKED_TEXT)
                    if S.read_w(addr) != want:
                        S.write_w(addr, want, len(original))
        except Exception as e:
            out.append(f"level name text failed: {e!r}")

        # The Hindenbird's goal line renders correctly only in Tazland, and it
        # overruns into strings other screens use, so it goes on there and
        # comes back off everywhere else.
        try:
            if self.mode == "open" and lid == 18 and not self.goal_met():
                p, b, u = self.goal_remaining()
                S.set_hindenbird_text(S.hindenbird_goal_text(p, b, u))
            elif S.hb_backup_taken():
                S.restore_hindenbird()
        except Exception as e:
            out.append(f"Hindenbird text failed: {e!r}")

        # The game's own three message boxes -- 100 sandwiches, the
        # destruction bonus, the Golden Sam Statue. Each fires when the player
        # completes something that is now an AP check, so each was announcing
        # a bonus game or a bounty the seed may have replaced with somebody
        # else's item. The sandwich one was the worst of the three: it appears
        # whether or not a portal was ever granted.
        #
        # Written on entering a level rather than when the check fires. The
        # renderer resolves a subtitle's id once, at raise time, so by the
        # moment the check goes off the box is already up saying the old words.
        try:
            if lid != getattr(self, "_bonus_text_level", None):
                self._bonus_text_level = lid
                for kind in N.BONUS_MSG:
                    N.set_bonus_text(
                        kind, N.bonus_line(kind, self.bonus_scout(lid, kind)))
        except Exception as e:
            out.append(f"bonus message text failed: {e!r}")

        # The advice on each boss door. Only rewritten while the player is in
        # that boss's hub -- the same rule as the Hindenbird's goal line, and
        # for the same reason: the text is only visible there, and leaving it
        # changed elsewhere disturbs screens that share the region.
        # The advice on each boss door.
        #
        # Both modes go through the same path now. A door has FIVE lines and
        # the game picks between them by which level the player was in last
        # (see BOSS_DOOR in game.py), so all five are set together -- writing
        # one of them, which is what this did, put the text somewhere the
        # player almost never saw. That is why Linear never showed a poster
        # count, and why Open only worked once its levels were finished.
        #
        # Re-asserted every tick against what is actually in the table, not
        # against what we last wanted. The old guard compared the message to
        # the previous message, so anything the game did afterwards stood.
        try:
            self._track_hub_reload(lid)
            for boss_id, door in S.BOSS_DOOR.items():
                want = (self._door_message(boss_id)
                        if door["hub"] == lid else None)
                out += self.game.boss_door_text(boss_id, want)
        except Exception as e:
            out.append(f"boss door text failed: {e!r}")

        # The title screen: "Start Game" becomes "Start AP", and the blurb
        # underneath becomes the version banner. Each entry carries its own
        # capacity, so they are written the same way rather than one by hand --
        # writing only the version is why "Start AP" never appeared.
        try:
            for slot, (addr, original, new, cap) in S.MENU_TEXT.items():
                if S.read_w(addr) != new:
                    S.write_w(addr, new, cap)
        except Exception as e:
            out.append(f"menu text failed: {e!r}")

        return out

    def _track_hub_reload(self, lid):
        """Notice a boss unlock that arrived while its hub was already built.

        The door is decided when the hub is constructed, so an unlock landing
        afterwards does not open it. Walking out to one of that hub's own
        levels and back does not rebuild it either -- which is the case that
        caught this: the unlock arrived in the Samsonian Museum, the door was
        still shut on the way out, and the line above it had already gone back
        to the vanilla advice.

        Cleared by entering the arena, which proves the door opened, or by
        any real rebuild of the hub. A rebuild is an arrival that FOLLOWS a
        departure taken after the unlock landed -- walking back into a level
        and out again is enough, and is how a player fixes it in practice.
        The arrival that started this does not count: the unlock landed while
        the player was already inside the Museum, so the hub they walked out
        into was the one they left behind before it.
        """
        departed = getattr(self, "_boss_departed", None)
        if departed is None:
            departed = self._boss_departed = set()

        prev = getattr(self, "_prev_lid", lid)
        self._prev_lid = lid

        # Bosses already flagged, handled BEFORE anything new is flagged. A
        # departure only counts if it was taken after the unlock landed, and
        # one taken in the same tick the unlock arrives was not -- the player
        # was already on their way out when it came in.
        if prev != lid:
            for boss_id in list(self.boss_needs_reload):
                hub = S.BOSS_HINT_HUB.get(boss_id)
                if hub is None:
                    continue
                if lid == boss_id:
                    # Standing in the arena: the door plainly opened.
                    self.boss_needs_reload.discard(boss_id)
                    departed.discard(boss_id)
                elif prev == hub and lid != hub:
                    # Left the hub after the unlock, so whatever gets built
                    # when the player returns is built from the current flags.
                    departed.add(boss_id)
                elif lid == hub and boss_id in departed:
                    self.boss_needs_reload.discard(boss_id)
                    departed.discard(boss_id)

        seen = getattr(self, "_bosses_seen", None)
        if seen is None:
            seen = set(self.bosses)
        for boss_id in self.bosses - seen:
            hub = S.BOSS_HINT_HUB.get(boss_id)
            if hub is None:
                continue
            if lid == hub or lid in D.HUB_LEVELS.get(hub, ()):
                self.boss_needs_reload.add(boss_id)
                departed.discard(boss_id)
        self._bosses_seen = set(self.bosses)

    def _door_message(self, boss_id):
        """What this boss's door should say, or None to leave the game's own.

        None matters as much as the text does: once the player can actually
        fight the boss, the game's own line is better advice than anything
        here -- it tells them to jump in the snowblower.
        """
        hub = S.BOSS_DOOR[boss_id]["hub"]
        if self.mode == "linear":
            # Linear has no unlock items; the poster gates are the lock, so
            # the only thing worth saying is how many are still missing.
            gate = self._gate_for(hub)
            if gate is None:
                return None
            # Received, exactly as linear_open_bosses counts it. The number on
            # the door has to be the number the door is actually waiting for,
            # or it tells the player to go and do the wrong thing.
            left = max(0, gate - self.posters)
            if left:
                return S.gate_message("linear", left, "hub_boss")
        elif boss_id not in self.bosses:
            return S.BOSS_HINT[boss_id]["locked"]
        # Granted, or the gate is met -- but if this hub was built before that
        # happened, the door in it is still shut, and telling the player to
        # jump in the cement mixer is advice that cannot work.
        if boss_id in self.boss_needs_reload:
            return BOSS_RELOAD_TEXT
        return None

    def _gate_for(self, lid):
        return {3: int(self.opt.get("gate_elephant_pong", 0)),
                8: int(self.opt.get("gate_gladiatoons", 0)),
                13: int(self.opt.get("gate_dodge_city", 0)),
                18: int(self.opt.get("gate_disco_volcano", 0))}.get(lid)

    # ---------------------------------------------------------------- deaths

    def boss_loss_tick(self):
        """Losing a boss fight, if the player asked for it to count.

        The master switch is checked here as well as the source toggle. It was
        not, which would have sent boss losses to a multiworld with DeathLink
        turned off entirely -- and the option text promises otherwise.
        """
        o = self.opt
        if not o.get("death_link") or not o.get("death_link_boss_losses"):
            return False
        return self.game.boss_lost()

    def on_death(self, kind):
        """Should this death be sent?

        Void deaths have an amnesty, so a player who falls a lot is not
        constantly killing the rest of the multiworld.
        """
        o = self.opt
        if not o["death_link"]:
            self.last_death_skipped = "Death Link is off"
            return False
        # Three independent toggles now, rather than one three-way choice --
        # boss losses do not fit on the captures/voids axis at all.
        if kind == "captures" and not o.get("death_link_captures", True):
            self.last_death_skipped = "captures are not a source"
            return False
        # Drowning and falling are one thing to the yaml; only the message
        # tells them apart.
        if kind in ("void_out", "drown"):
            if not o.get("death_link_void_outs", True):
                self.last_death_skipped = "void outs are not a source"
                return False
            self.void_seen += 1
            need = int(o.get("void_out_amnesty", 1))
            if self.void_seen < need:
                self.last_death_skipped = (
                    f"amnesty: {self.void_seen} of {need} void deaths")
                self.save_state()
                return False
            self.void_seen = 0
        self.save_state()
        return True

    def can_apply_death(self):
        """Is this a moment an incoming DeathLink can be acted on?

        Not while Taz is the mouse or the ball. Those are Taz: Haunted's own
        transformations, and teleporting one of them to the level start lands
        a shape that has no business being there -- the ball cannot use a door
        and cannot be spun out by the player, so it is a soft lock rather than
        a death. The link is HELD, not dropped: the moment he is himself again
        it is applied.
        """
        return self.game.taz_state() not in G.TRANSFORM_STATES

    def apply_death(self):
        """An incoming DeathLink.

        Back to the start of the level -- or, in a boss arena, losing the
        fight, because there is no level start to go back to.
        """
        g = self.game
        if g.is_boss():
            until = g.start_boss_loss()
            if until:
                self.boss_loss_until = until
                return True
            return False
        lid = g.level_id()
        rec = self.spawns.get(str(lid))
        if rec:
            return g.teleport_to(rec["pos"])
        return False

    # ---------------------------------------------------------------- tick

    def save_file_prompt(self, g):
        """Tell the player what they have to do before anything happens.

        Said once per change, never once per tick -- this runs on the title
        screen, which is exactly where a player sits reading the log.

        The first time on a seed there is nothing to name, so it asks for a
        file. After that the chosen file is remembered per seed, and the
        prompt names it -- loading a DIFFERENT file would hand this seed's
        items to a save that has never seen them.
        """
        picked = g.file_selected()
        # BOTH halves matter. Players are told to connect on the Choose
        # Language screen, which is before the game has written anything --
        # so the file byte can read a perfectly plausible 0 while no file
        # exists at all. Requiring a loaded level as well means that reads as
        # "not started", which is what it is, and stops a phantom file 0
        # being remembered as the player's choice and then asked for by name
        # for the rest of the seed.
        if picked is not None and g.in_world():
            self._file_prompt = None
            if picked != getattr(self, "last_file", None):
                self.last_file = picked
                self.save_state()
            return []

        last = getattr(self, "last_file", None)
        want = ("Select Save File to Begin" if last is None else
                f"Load File {last + g.FILE_DISPLAY_BASE} to Continue")
        if want == getattr(self, "_file_prompt", None):
            return []
        self._file_prompt = want
        return [want]

    def tick(self):
        """One pass. Split by what is safe where.

        Two different kinds of work happen here, and conflating them was a
        mistake: the title screen has no save file, so anything that writes to
        the save region has to wait, but the text on screen and the difficulty
        check are exactly the things a player needs BEFORE they start a file.
        Returning early when no file is loaded skipped both.
        """
        out = []
        g = self.game
        if not g.alive():
            return out

        # FIRST, before the demo checks and before the save-file gate.
        #
        # The bonus gate decides whether a police box is CONSTRUCTED, and it
        # decides it once, while the map is being built. So it has to be
        # patched before the first map load of the session -- not after.
        #
        # It used to sit below the in_world() gate, which meant it could not
        # run until a level was already loaded. Boot the game, pick a file,
        # walk into Yosemite Zoo: the hub was built by the SHIPPING gate,
        # which answers from the sandwich count, so every level sitting at
        # 100 sandwiches got a police box regardless of what the server had
        # granted. The patch then landed a tick later and could not un-build
        # them. The ten-second DEMO_SETTLE window made it worse, because it
        # covers exactly the moment a player picks a file.
        #
        # Safe this early: it writes seven words of game code and nine bytes
        # of scratch at 0x01F00A00. It never touches the save region, which
        # is the only thing the title screen cannot tolerate.
        out += g.bonus_gate_tick(self.bonus)

        # Vitals, before anything can return early -- a freeze or a stuck
        # slow-motion does not wait for a save file to be loaded.
        if self.health is not None:
            try:
                out += self.health.tick()
            except Exception as exc:
                N.log.debug("health: %s", exc)

        # The attract-mode demo plays real levels by itself, and the save
        # data it leaves behind is still there for a moment after it ends --
        # which is how returning to the title screen sent every Zooney Tunes
        # check at once.
        #
        # So it is not enough to skip while the demo runs; nothing is trusted
        # for a while after it stops either.
        if g.demo_running():
            self._demo_until = time.time() + DEMO_SETTLE
            return out
        if time.time() < getattr(self, "_demo_until", 0.0):
            return out

        lid = g.level_id()
        if lid != self._last_level:
            self._last_level = lid
            if self._gate_warning:
                out.append(self._gate_warning)
                self._gate_warning = None
            # On the title screen and again on every level change, so a
            # mismatch is noticed before it costs the player any checks.
            out += self.check_difficulty()
            self._seeded = False

        # --- safe anywhere, including the title screen --------------------
        out += self.update_text()

        # In-game notifications. They wait until the player actually has
        # control -- GAME_STATE 1 with a file loaded, the attract demo
        # already handled above -- so nothing lands on a loading screen,
        # a cutscene or the pause menu; the queue just holds.
        #
        # Wrapped because this is cosmetic: a notification that goes wrong
        # must never stop checks being sent.
        try:
            was = self.notify.mode
            self.notify.tick(
                g.game_state() == T.STATE_ACTIVE and g.in_world())
            if self.notify.mode != was:
                self.save_state()
        except Exception as exc:
            # Cosmetic: it must never interrupt checks, and it must never
            # talk to the player -- the box on screen is the whole point.
            N.log.debug("in-game text: %s", exc)

        # --- everything below needs a loaded save file --------------------
        #
        # Two conditions, and they are not the same thing. in_world() only
        # says a real level is loaded. file_selected() says the player has
        # actually chosen a file -- which used to be unaskable, because a
        # u32 read of the signed -1 the title screen holds came back as 255
        # and was reported as file 0.
        #
        # Nothing is handed over until both are true. An item written into
        # the save region before a file exists is written into somebody's
        # file 0.
        out += self.save_file_prompt(g)
        if g.file_selected() is None or not g.in_world():
            return out

        g.refresh_save_file()

        # Read before anything writes to the completion flags: they double as
        # the boss gate in Open, so enforce_access clears the ones the player
        # has not earned and the check would never see them.
        self.completed |= g.read_completions()

        # Outside the once-per-file seeding, because true_sandwiches needs it
        # on every session and not only the one that did the seeding.
        g.starting_sandwiches = int(self.opt.get("starting_sandwiches", 0))
        if not self._seeded:
            g.seed_sandwiches(int(self.opt.get("starting_sandwiches", 0)))
            self._seeded = True

        # Every granted item, applied fresh. Cheap, and it means a reload or a
        # save state cannot leave the world half configured.
        if self.mode == "open":
            g.enforce_access(self.levels, self.granted_bosses(), "open")
        else:
            open_bosses = self.linear_open_bosses()
            g.enforce_access(open_bosses, open_bosses, "linear")
            g.enforce_linear_gate(open_bosses)
        g.enforce_costumes(self.costumes)
        # Disco Volcano runs straight into The Hindenbird, so its exit is
        # pointed back at the hub while the Hindenbird is locked.
        if self.mode == "open":
            g.enforce_flow(self.hindenbird_requirements_met())

        if self.boss_loss_until:
            if time.time() < self.boss_loss_until:
                g.hold_boss_loss()
            else:
                self.boss_loss_until = 0.0
        self._demo_until = 0.0
        self.last_death_kind = None
        self.last_death_skipped = None
        # Doors that cannot be locked through the access field are held by
        # position instead.
        # Geofences are an Open-mode mechanism and must not run in Linear.
        #
        # They exist because some doors cannot be locked through the access
        # field: Zooney Tunes has to stay marked as a hub or hub 1's other two
        # doors do not render at all, and Tazland is natively open. Standing in
        # front of one and being pushed away is the workaround.
        #
        # Linear has no such problem -- the game locks its own doors -- so
        # running them there just walls the player out of levels they are
        # entitled to.
        if self.mode == "open":
            out += g.enforce_gates(self.levels, self.granted_bosses(),
                                   self.spawns)
            # Tazland's completion flag is only set by entering Disco Volcano,
            # and Open mode blocks that door -- so the check would be
            # unreachable. Walking into it with all seven posters destroyed is
            # the same accomplishment, so it counts.
            #
            # Linear needs none of this: the door is not blocked there, so the
            # player walks in and the game sets the flag itself.
            if (g.touched_disco_volcano and TAZLAND not in self.completed
                    and g.posters_done(TAZLAND)):
                self.completed.add(TAZLAND)
                self.save_state()

        # Catchers are identified by where they stand, not by a saved flag, so
        # they have to be watched while playing rather than read afterwards.
        # The banked set goes in so keepers already checked can be despawned.
        for kill in g.catcher_tick(self.catcher_kills):
            if kill in self.catcher_kills:
                # Correct, and the last place a takedown could vanish without
                # a word: this check has already been sent, so there is
                # nothing to send. Worth saying anyway, because from the
                # player's side "I beat it and nothing happened" looks
                # identical whether the reason is this or a bug -- and a
                # banked catcher is normally despawned before they can even
                # reach it, so getting here at all is mildly odd.
                said = (f"{D.LEVEL_NAME.get(kill[0], kill[0])} catcher "
                        f"{kill[1] + 1} was already checked -- nothing to "
                        f"send. (It should have been despawned; if you are "
                        f"still fighting it, say so.)")
                if said != getattr(self, "_already_said", None):
                    self._already_said = said
                    out.append(said)
                continue
            refused = self.catcher_refused(kill[0])
            if refused:
                out.append(f"{ERROR_PREFIX}Catcher check in "
                           f"{D.CATCHER_LEVEL_NAME.get(kill[0], kill[0])}"
                           f" not sent: "
                           f"{refused}")
                # The judge banks an index the moment it fires, so without
                # this it goes on believing that catcher is done and beating
                # it again -- after the costume finally arrives -- produces
                # nothing at all, for the rest of the session.
                g.uncredit_catcher(*kill)
                continue
            self.catcher_kills.add(kill)
            self.save_state()

        # THE LOG, NOT THE CLIENT. Both of these are diagnostics: they say
        # why the judge did not credit something, and every one of them is a
        # sentence about the client's own reasoning rather than about the
        # game. That was worth putting in front of the player while catchers
        # were unreliable and a missing check needed explaining. They are
        # reliable now, so it is noise -- and one of these lines, repeating
        # once a minute, is what prompted moving them.
        #
        # Nothing is lost. `logs/` keeps them, timestamped, and they read
        # better there anyway: several in a row with the trace between them
        # tells you far more than one a minute in a chat window.
        #
        # If a catcher genuinely stops sending, these are the first thing to
        # read. The rate limit is gone with the move -- a log can take it.
        for lid, idx, why in g.catcher_lost:
            if (lid, idx) in self.catcher_kills:
                continue
            N.log.warning("catcher: a takedown in %s was not counted: %s",
                          D.CATCHER_LEVEL_NAME.get(lid, lid), why)
        for lid, said in g.catcher_blind:
            N.log.warning("catcher: in %s, %s",
                          D.CATCHER_LEVEL_NAME.get(lid, lid), said)
        if g.catcher_why:
            N.log.debug("catcher: %s", " | ".join(g.catcher_why))
        # The bonus game portals, decided from the granted list instead of
        # from a sandwich count -- see the comment above BONUS_GATE in
        # game.py. This runs FIRST so the table is current before anything
        # else can trigger a load, and it never raises: a gate that will not
        # patch leaves the game exactly as it shipped, and the sandwich spoof
        # below still makes the portals appear.
        # Zooney Tunes and Looningdale's show a prompt book on Standard that
        # no other difficulty shows. Held off, so Standard reads the same as
        # the rest -- one word, and the game's own switch for it.
        out += g.prompt_gate_set(bool(self.opt.get("hide_standard_hints",
                                                   True)))
        g.sandwich_tick(self.bonus)
        # Holds the slow motion off a bounty banner the client raised. Does
        # nothing at all unless one is up, and never touches the game's own.
        g.bounty_tick()
        # The completion flags need the same protection, and for the same
        # reason: the client writes them, so the field is not evidence.
        g.completion_tick()

        # Filler and traps. One at a time: they share the powerup fields, so
        # two at once means the second overwrites the first's bookkeeping and
        # neither ends cleanly.
        for finished in g.hold_traps(self.active_traps):
            self.active_traps.pop(finished, None)
            if finished in g.POWERUPS:
                g.end_powerup(finished)
            elif finished == "squash":
                # The squash ends by putting a bit back, not by clearing a
                # powerup -- it is not one.
                g.end_squash()
        # One at a time, and not instantly one after another: see EFFECT_GAP.
        if (self.effect_queue and not self.active_traps
                and time.time() >= self._effect_after):
            effect = self.effect_queue[0]
            until = g.grant_effect(effect)
            if until == "defer":
                pass          # mid-spin, or on a rollercoaster; try again
            else:
                self.effect_queue.popleft()
                self._effect_after = time.time() + G.Game.EFFECT_GAP
                if until:
                    self.active_traps[effect] = until

        # Dying as the ball leaves Taz as the ball. death_tick arms the
        # recovery; this keeps asking for the spin until it takes.
        said = g.unball_tick()
        if said:
            out.append(said)

        kind = g.death_tick()
        if kind:
            self.last_death_skipped = None
        if kind and self.on_death(kind):
            self.deaths_pending += 1
            self.last_death_kind = kind
        elif kind and self.last_death_skipped:
            out.append(f"{ERROR_PREFIX}Death not sent ({kind}): "
                       f"{self.last_death_skipped}")
        if self.boss_loss_tick():
            self.deaths_pending += 1
            self.last_death_kind = "boss loss"

        # Checks are announced by Archipelago itself, so naming them here
        # would print each one twice.
        self.new_checks()
        return out

    def bonus_message_locations(self):
        """The location ids behind the game's three own message boxes.

        At most one of each per level, and only the ones this seed actually
        contains: a sandwich check at 100 exists at every interval, but the
        destruction check the game announces is the one at the Daffy-culty's
        target, which is not there at all if destruction checks are off.
        """
        goal = D.DESTRUCTION_GOAL.get(self.opt.get("difficulty"), 50)
        out = {}
        for loc in self.locations:
            lid, t = loc.get("level"), loc.get("type")
            if t == "sandwich" and loc.get("threshold") == D.SANDWICH_GOAL:
                out[(lid, "sandwich")] = loc["id"]
            elif t == "destruction" and loc.get("threshold") == goal:
                out[(lid, "destruction")] = loc["id"]
            elif t == "statue":
                out[(lid, "statue")] = loc["id"]
        self._bonus_ids = out
        return sorted(set(out.values()))

    def bonus_scout(self, lid, kind):
        """(item, player) for one of those, or None.

        None is the ordinary case early on -- the scouts arrive a moment after
        connecting -- and also the right answer when the seed has no such
        check in this level at all.
        """
        if not hasattr(self, "_bonus_ids"):
            self.bonus_message_locations()
        loc = self._bonus_ids.get((lid, kind))
        return self.bonus_scouts.get(loc) if loc is not None else None

    def catcher_refused(self, lid):
        """Why a catcher check in this level must not be sent, or None.

        A last line of defence, and deliberately outside the detection: even
        a perfect judge is reading a game that can be interrupted, and these
        two facts are known here for certain rather than inferred from memory.

        THE COSTUME. Beating a keeper costs Taz whatever he is wearing, so a
        catcher cannot be beaten without the level's costume. That is already
        the generator's logic rule, which means a check sent without the
        costume is not merely early -- it is out of logic, and it happened: a
        Cartoon Strip Mine catcher sent for a costume that had not arrived.

        THE LEVEL. In Open mode the doors are ours to lock, so a check from a
        level the player cannot enter is equally impossible. Linear gets no
        such test: it has no level unlock items at all, and asking for one
        there would refuse every catcher in the seed.

        The unlock test is limited to levels that HAVE an unlock. Yosemite Zoo
        is a hub with a keeper in it, and hubs are never items -- testing it
        the same way would have refused that check for the whole seed.
        """
        costume = D.LEVEL_COSTUME_NAME.get(lid)
        # Only a costume that is actually in the pool. The hub's Christmas
        # Reindeer is, so the hub keeper is covered too.
        if costume and costume in D.COSTUMES:
            if G.COSTUME_BY_NAME.get(costume) not in self.costumes:
                return f"the {costume} costume has not arrived"
        if (self.mode == "open" and lid in D.LEVEL_ORDER
                and lid not in self.levels):
            return f"{D.LEVEL_NAME.get(lid, lid)} is not unlocked"
        return None

    def new_checks(self):
        """Locations satisfied now that have not been sent."""
        done = self.game.satisfied(self.locations, self.catcher_kills)
        # Completion is remembered rather than read, because the flag is put
        # straight back to zero.
        done |= {l["id"] for l in self.locations
                 if l["type"] == "completion" and l["level"] in self.completed}
        new = sorted(done - self.sent)
        if new:
            self.sent |= set(new)
            self.save_state()
        return new

    def check_difficulty(self):
        """A mismatch is returned marked, so the client can log it as an error.

        The message is prefixed rather than logged here: this layer has no
        logger, and the prefix lets the caller choose the level -- which is
        what makes it red in the client.
        """
        want = self.opt["difficulty"]
        have = self.game.difficulty()
        if have is None or have == want:
            self._warned_difficulty = False
            return []
        self._warned_difficulty = True
        return [f"{ERROR_PREFIX}{DIFFICULTY_WARNING}\n"
                f"  yaml: {want}, game: {have}"]

    # ---------------------------------------------------------------- run

    def run(self):
        if not self.game.connect():
            print("  could not reach PCSX2 -- is it running with PINE on?")
            return
        print(f"\n  {self.mode} mode, {len(self.locations)} location(s)")
        print(f"  {len(self.sent)} already sent\n")
        try:
            while True:
                time.sleep(TICK)
                for line in self.tick():
                    print(f"  {line}")
        except KeyboardInterrupt:
            self.save_state()
            print("\n  stopped; state saved.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", help="a json file of option overrides")
    ap.add_argument("--seed", default="local")
    ap.add_argument("--dry", action="store_true",
                    help="show what a seed would contain and exit")
    a = ap.parse_args()

    raw = load_json(a.yaml, {}) if a.yaml else {}
    c = Client(raw, a.seed)

    if a.dry:
        print()
        print(O.summary(c.opt))
        print(f"  locations       {len(c.locations)}")
        for w in c.opt["warnings"]:
            print(f"  ! {w}")
        print()
        return
    c.run()


if __name__ == "__main__":
    main()
