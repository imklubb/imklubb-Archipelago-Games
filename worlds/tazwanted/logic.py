"""
logic.py -- locations, items, options and rules.

Four things that only make sense together: what the locations are, what items
fill them, what the yaml means, and what it takes to reach each one. None of it
touches memory or Archipelago, so the whole file can be run on its own -- see
the self-test at the bottom.

The sections keep their original prefixes (D for data, I for items, O for
options, R for rules) so the code reads the same as when they were separate
files; they all point at this module.
"""

import sys as _sys

# Each section refers to the others by prefix, and every one of them is now
# this module. Assigning here rather than rewriting hundreds of references
# keeps the code readable and the diff small.
D = I = O = R = T = _sys.modules[__name__]


# ==========================================================================
# SAVE FORMAT
#
# Offsets, the level table and the block arithmetic. Facts about
# the save file rather than live access, so they live here: the
# location table needs them during generation, when there is no
# emulator to read from.
# ==========================================================================

FILE_STRIDE     = 0x42B4

COSTUME_NONE = 0xFF

LEVEL_IDS = {
    3: "Yosemite Zoo", 4: "Ice Burg", 5: "Zooney Tunes", 6: "Looney Lagoon",
    7: "Elephant Pong", 8: "Sam Francisco", 9: "Looningdale's",
    10: "Samsonian Museum", 11: "Bank of Samerica", 12: "Gladiatoons",
    13: "Wile E. West", 14: "Taz: Haunted", 15: "Cartoon Strip-Mine",
    16: "Granny Canyon", 17: "Dodge City", 18: "Tazland A-maze-ment Park",
    19: "Disco Volcano", 20: "The Hindenbird",
}

HUBS = {3, 8, 13}

POSTER_LEVELS = [4, 5, 6, 9, 10, 11, 14, 15, 16, 18]

SAVE_BASE   = 0x400444

SAVE_STRIDE = 0x238

FIRST_LEVEL_ID = 3

L_COMPLETE      = 0x000     # boss levels: "<Boss> Defeated"

L_SANDWICH_BITS = 0x004     # 480 bytes, one dword per sandwich

L_SANDWICHES    = 0x1E4

L_POSTER        = 0x1E8     # 7 bitflags, 4 bytes apart

L_POSTERS_DONE  = 0x210

L_BOUNTY_DEDUCT = 0x214

L_TOTAL_BOUNTY  = 0x218

L_DESTRUCTION   = 0x21C     # percent, 0-100

L_GOLDEN_SAM    = 0x228

L_BONUS_GAME    = 0x230

L_SECONDS       = 0x234

L_ACCESS        = 0x224

ACCESS_LOCKED   = 0x00

ACCESS_LEVEL    = 0x20

ACCESS_HUB      = 0x21

HUB_LEVELS = {3: [4, 5, 6], 8: [9, 10, 11], 13: [14, 15, 16]}

HUB_BOSS   = {3: 7, 8: 12, 13: 17}

POSTERS_PER_LEVEL = 7

SANDWICH_GOAL = 100

POSTER_NAMES = {
    "Ice Burg": ["Ice Rink", "Slippery Ice", "Fridge",
                 "Ice Cream Truck on Cliff", "Cable Car", "Igloo",
                 "Christmas Tree"],
    "Zooney Tunes": ["House", "Van on Cliff", "Behind Waterfall",
                     "Watchtower", "Cork in Log", "Bee Hive", "Tree Trunk"],
    "Looney Lagoon": ["Life Ring", "Pirate Ship", "Lighthouse", "Sink",
                      "Washing Machine", "Gazebo", "Plank"],
    "Looningdale's": ["Cuckoo Clock", "Toy Room Kite",
                      "Jumbotron", "Explosives Room Detonator",
                      "Underground TVs", "Lobby Rafters",
                      "Grocery Store Floor"],
    "Samsonian Museum": ["Floor Cleaner", "Bathroom Mirror", "Lightbulb",
                         "Spinning Coin", "Library", "Sewer Drain",
                         "Control Room"],
    "Bank of Samerica": ["Red Skyscraper Construction", "Green Steel Beam",
                         "Wrecking Balls", "Hanging Cement Truck",
                         "Radio Tower", "Traffic Gate", "Wet Cement Barrel"],
    "Taz: Haunted": ["Haunted House 2nd Story Window", "Pillar Gong",
                    "Haunted House Rooftop", "Overlooking Entrance",
                    "Saloon Balcony", "Lab", "Roller Coaster"],
    "Cartoon Strip-Mine": ["Mining Drill", "Spotlight", "Tunnels",
                           "Money Machine", "Mine Cart", "Underground Frogger",
                           "Treasure Room"],
    "Granny Canyon": ["Train", "Cannon", "Gas Station", "Water Tower",
                      "Walk the Plank to the Cave", "Workshop", "Cacti"],
    "Tazland A-maze-ment Park": ["Saw Room", "Giant Egg", "Wrecking Ball",
                                 "Jet Engine", "Cave Cliff", "Monkey Garden",
                                 "Rolling Logs"],
}

NO_BONUS_GAME = {18}

def level_block(level_id, save_file=0):
    return (SAVE_BASE + (level_id - FIRST_LEVEL_ID) * SAVE_STRIDE
            + save_file * FILE_STRIDE)

def poster_addr(level_id, index, save_file=0):
    """index is 0-6, matching POSTER_NAMES order."""
    if not 0 <= index < POSTERS_PER_LEVEL:
        raise ValueError(f"poster index must be 0-6, got {index}")
    return level_block(level_id, save_file) + L_POSTER + index * 4

def access_addr(level_id, save_file=0):
    """The +0x224 access field for any level, hub or boss."""
    return level_block(level_id, save_file) + L_ACCESS

HUBS = [3, 8, 13]

SANDWICH_GOAL = 100

# ==========================================================================
# LOCATIONS AND DETECTION
# ==========================================================================

#!/usr/bin/env python3
"""
taz_data.py -- every location and item, with stable IDs and how to detect them.

This is the layer both modes share. Nothing here depends on Open or Linear, on
the player's goal, or on which options they picked -- those decide which of
these locations exist in a given seed, not what they are or what they are
called.

STABLE IDS

Location IDs must never move. A seed generated today has to mean the same thing
next month, so each category gets a reserved block sized for its maximum, and
options only decide how much of a block is used. Turning sandwich checks from
100 down to 5 fills more of the sandwich block; it does not renumber anything
else.

    posters       70    fixed
    sandwiches  1000    10 levels x up to 100 checks
    destruction 1000    10 levels x up to 100 checks
    statues       10    fixed
    bonus games    9    fixed
    catchers      44    fixed
    bosses        10    5 defeats + 5 Hindenbird Tickets

DETECTION

Each location carries what the client needs to check it: an address computed
from the save block, and a rule. Nothing here reads memory -- the client does
that -- so this module is importable without an emulator.
"""


# Archipelago location and item IDs start here. Chosen once; never change it.
BASE_ID = 8_140_000

# Levels that hold collectables, in a fixed order. The order fixes the IDs, so
# it must not be rearranged.
LEVELS = [
    (4,  "Ice Burg"),
    (5,  "Zooney Tunes"),
    (6,  "Looney Lagoon"),
    (9,  "Looningdale's"),
    (10, "Samsonian Museum"),
    (11, "Bank of Samerica"),
    (14, "Taz: Haunted"),
    (15, "Cartoon Strip-Mine"),
    (16, "Granny Canyon"),
    (18, "Tazland A-maze-ment Park"),
]
LEVEL_NAME = {lid: name for lid, name in LEVELS}
LEVEL_ORDER = [lid for lid, _ in LEVELS]

# One event per boss, so Linear can require "this boss is beaten" without
# inventing an item the player would see.
BEATEN_EVENT = {
    7: "Elephant Pong Beaten",
    12: "Gladiatoons Beaten",
    17: "Dodge City Beaten",
    19: "Disco Volcano Beaten",
}

HINDENBIRD_LEVEL = 20

BOSSES = [
    (7,  "BOSS 1: Gossamer Defeated"),
    (12, "BOSS 2: Daffy Defeated"),
    (17, "BOSS 3: Sam Defeated (Dodge City)"),
    (19, "BOSS 4: Sam Defeated (Disco Volcano)"),
    (20, "BOSS 5: Tweety Defeated"),
]

# Tazland has no bonus game.
NO_BONUS = {18}

# Destruction goal by difficulty. The option is what generates the seed; the
# client warns if the game is set differently.
DESTRUCTION_GOAL = {"standard": 50, "advanced": 75, "expert": 100}

SANDWICH_GOAL = 100
POSTERS_PER_LEVEL = 7

# Reachable in Tazland before the bridge, measured in game. Only Standard has
# any sandwiches there, and 2% destruction is below the smallest check
# interval, so no destruction check is ever in logic before the bridge.
TAZLAND_PREGATE = {
    "standard": {"sandwiches": 9, "destruction": 2},
    "advanced": {"sandwiches": 0, "destruction": 2},
    "expert":   {"sandwiches": 0, "destruction": 2},
}

# Reserved ID blocks, in order. Sizes are maximums, not what a seed uses.
BLOCKS = [
    ("poster", 70),
    ("sandwich", 1000),
    ("destruction", 1000),
    ("statue", 10),
    ("bonus", 9),
    ("catcher", 50),
    ("boss", 5),
    ("ticket", 5),
    ("completion", 10),
]
BLOCK_START = {}
_off = 0
for _name, _size in BLOCKS:
    BLOCK_START[_name] = BASE_ID + _off
    _off += _size
TOTAL_RESERVED = _off

# Sandwiches and destruction used to share one stride and one slot function,
# which worked only while both had the same finest interval. A sandwich check
# every 1 breaks that: sandwiches now need 100 slots a level and destruction
# still needs 20, so they are separate and each says what it is.
SANDWICH_SLOTS_PER_LEVEL = 100      # the finest interval is 1, so 100/1
DESTRUCTION_SLOTS_PER_LEVEL = 100   # a percentage, and now also every 1%


# ---------------------------------------------------------------- helpers


def _level_index(lid):
    return LEVEL_ORDER.index(lid)


def poster_id(lid, n):
    """n is 1..7."""
    return BLOCK_START["poster"] + _level_index(lid) * POSTERS_PER_LEVEL + n - 1


def _sandwich_slot(threshold):
    """A sandwich count, 1..100, to a slot 0..99.

    Deriving the id from the THRESHOLD rather than from a running count is what
    keeps it stable: "100 Sandwiches" is the hundredth check at an interval of
    1 and the first at an interval of 100, but it is the same location and must
    keep the same id either way.
    """
    return threshold - 1


def _destruction_slot(threshold):
    """A percentage, 1..100, to a slot 0..99.

    Was threshold // 5 - 1 while the finest interval was 5. Checks every 1%
    means every whole percentage is its own location, so the slot is the
    percentage itself -- and, as with sandwiches, derived from the THRESHOLD
    so that 50% keeps one id whether it is the tenth check or the fiftieth.
    """
    return threshold - 1


def sandwich_id(lid, threshold):
    return (BLOCK_START["sandwich"]
            + _level_index(lid) * SANDWICH_SLOTS_PER_LEVEL
            + _sandwich_slot(threshold))


def destruction_id(lid, threshold):
    return (BLOCK_START["destruction"]
            + _level_index(lid) * DESTRUCTION_SLOTS_PER_LEVEL
            + _destruction_slot(threshold))


def completion_id(lid):
    return BLOCK_START["completion"] + _level_index(lid)


def statue_id(lid):
    return BLOCK_START["statue"] + _level_index(lid)


def bonus_id(lid):
    return BLOCK_START["bonus"] + _level_index(lid)


def boss_id(i):
    """i is 0..4, in BOSSES order."""
    return BLOCK_START["boss"] + i


def ticket_id(i):
    return BLOCK_START["ticket"] + i


# ---------------------------------------------------------------- thresholds


def sandwich_thresholds(interval, start=0):
    """The counts at which a sandwich check fires.

    Starting sandwiches both shift the thresholds and remove locations: with a
    start of 25 and an interval of 5, the checks are 30, 35 ... 100, so 15 of
    them rather than 20. The player is not handed the first 15 for free.
    """
    if not interval:
        return []
    return [v for v in range(interval, SANDWICH_GOAL + 1, interval)
            if v > start]


def destruction_thresholds(interval, goal, start=0):
    """The percentages at which a destruction check fires.

    The Daffy-culty's target always pays out. An interval that does not
    divide it leaves a remainder, and that remainder is a check of its own --
    on Expert, checks every 75% is two checks, at 75% and at 100%, rather
    than one at 75% and nothing at all for the last quarter of the level.

    That also settles the cases that used to stop short: Advanced with checks
    every 50% is 50% and 75%, not just 50%.
    """
    if not interval:
        return []
    out = [v for v in range(interval, goal + 1, interval) if v > start]
    if goal > start and goal not in out:
        out.append(goal)
    return out


# ---------------------------------------------------------------- locations


def poster_locations():
    out = []
    for lid, name in LEVELS:
        names = T.POSTER_NAMES.get(name, [])
        for n in range(1, POSTERS_PER_LEVEL + 1):
            label = names[n - 1] if n <= len(names) else f"Poster {n}"
            out.append({
                "id": poster_id(lid, n),
                "name": f"{name} - Poster - {label}",
                "type": "poster", "level": lid, "index": n,
                "offset": T.L_POSTER + (n - 1) * 4,
                "rule": "nonzero",
            })
    return out


def sandwich_locations(interval, start=0):
    out = []
    for lid, name in LEVELS:
        for n, thr in enumerate(sandwich_thresholds(interval, start), 1):
            out.append({
                "id": sandwich_id(lid, thr),
                "name": f"{name} - {thr} Sandwiches",
                "type": "sandwich", "level": lid, "index": n,
                "offset": T.L_SANDWICHES, "threshold": thr,
                "rule": "at_least",
            })
    return out


def destruction_locations(interval, difficulty):
    goal = DESTRUCTION_GOAL[difficulty]
    out = []
    for lid, name in LEVELS:
        for n, thr in enumerate(
                destruction_thresholds(interval, goal), 1):
            out.append({
                "id": destruction_id(lid, thr),
                "name": f"{name} - {thr}% Destruction",
                "type": "destruction", "level": lid, "index": n,
                "offset": T.L_DESTRUCTION, "threshold": thr,
                "rule": "at_least",
            })
    return out


def completion_locations():
    """Finishing a level is a check.

    The flag has to be put back to zero afterwards: in Open mode the three
    per-hub completions ARE the boss gate, so leaving one set would open a
    boss the player has not earned.
    """
    return [{
        "id": completion_id(lid),
        "name": f"{name} - Level Complete",
        "type": "completion", "level": lid,
        "offset": T.L_COMPLETE, "rule": "nonzero",
    } for lid, name in LEVELS]


def statue_locations():
    return [{
        "id": statue_id(lid),
        "name": f"{name} - Golden Sam Statue",
        "type": "statue", "level": lid,
        "offset": T.L_GOLDEN_SAM, "rule": "nonzero",
    } for lid, name in LEVELS]


def bonus_locations():
    return [{
        "id": bonus_id(lid),
        "name": f"{name} - Bonus Game Completed",
        "type": "bonus", "level": lid,
        "offset": T.L_BONUS_GAME, "rule": "nonzero",
    } for lid, name in LEVELS if lid not in NO_BONUS]


# Levels in LEVELS, then any hub that has keepers. Appended so the existing
# catcher IDs do not move: inserting a hub at the front would renumber all
# forty-four.
CATCHER_LEVELS = [lid for lid, _ in LEVELS] + [3, 8, 13]
CATCHER_LEVEL_NAME = dict(LEVELS)
CATCHER_LEVEL_NAME.update({3: "Yosemite Zoo", 8: "Sam Francisco",
                           13: "Wile E. West"})


def catcher_locations(catchers):
    """catchers: the taz_catchers.json structure.

    Identified by where each keeper stands rather than by any stored flag, so
    these carry a position instead of an offset.
    """
    out = []
    for lid in CATCHER_LEVELS:
        name = CATCHER_LEVEL_NAME[lid]
        rec = catchers.get(str(lid))
        if not rec:
            continue
        for i, c in enumerate(rec["catchers"]):
            out.append({
                "id": BLOCK_START["catcher"] + _catcher_index(catchers, lid, i),
                "name": f"{name} - Catcher - {c['name']}",
                "type": "catcher", "level": lid, "index": i,
                "pos": c["pos"], "radius": rec.get("radius", 800.0),
                "rule": "defeated",
            })
    return out


def _catcher_index(catchers, lid, i):
    """A flat index across CATCHER_LEVELS, so IDs are stable."""
    n = 0
    for other in CATCHER_LEVELS:
        rec = catchers.get(str(other))
        if other == lid:
            return n + i
        if rec:
            n += len(rec["catchers"])
    return n + i


def boss_locations(with_tickets=False):
    """Every boss that is a CHECK.

    The Hindenbird is not one of them. Beating Tweety IS the goal, so a check
    there would hand out an item at the moment the run ends -- nobody would
    ever use it, and it makes the last boss the only one whose reward cannot
    matter.

    Leaving it out also removes a circle that no logic could resolve: the
    Hindenbird's own ticket counted towards the goal that gates the Hindenbird,
    and Archipelago's spheres and the tracker settled that loop differently.
    """
    out = []
    for i, (lid, name) in enumerate(BOSSES):
        if lid == HINDENBIRD_LEVEL:
            continue
        out.append({
            "id": boss_id(i),
            "name": name,
            "type": "boss", "level": lid,
            "offset": T.L_COMPLETE, "rule": "nonzero",
        })
        # A second check per boss only when bosses are part of the goal --
        # otherwise the tickets are meaningless and the boss is one location.
        if with_tickets:
            out.append({
                "id": ticket_id(i),
                "name": f"{name} - Hindenbird Ticket",
                "type": "ticket", "level": lid,
                "offset": T.L_COMPLETE, "rule": "nonzero",
            })
    return out


def all_locations(sandwich_interval=100, destruction_interval=50,
                  difficulty="standard", start_sandwiches=0,
                  catchers=None, with_tickets=False):
    """Every location a seed with these options contains."""
    out = []
    out += poster_locations()
    out += sandwich_locations(sandwich_interval, start_sandwiches)
    out += destruction_locations(destruction_interval, difficulty)
    out += statue_locations()
    out += bonus_locations()
    out += completion_locations()
    if catchers:
        out += catcher_locations(catchers)
    out += boss_locations(with_tickets)
    return out


def location_address(loc, save_file=0):
    """Where the client reads this location, or None for a catcher."""
    if "offset" not in loc:
        return None
    return T.level_block(loc["level"], save_file) + loc["offset"]

# ==========================================================================
# THE ITEM POOL
# ==========================================================================

#!/usr/bin/env python3
"""
taz_items.py -- the item pool, and how it scales with the options.

Every location needs an item, so the pool is built to match whatever the
options produced -- 158 locations by default, up to 538 with the finest
sandwich and destruction intervals.

THE POSTER SPLIT

Wanted Posters are the interesting case. A seed can have 70 of them while the
goal needs only 50, and marking all 70 as progression makes the generator work
far harder than it needs to: it has to treat every one as potentially gating
something. So exactly as many as the goal requires are progression, and the
rest are useful. The same trick made Toy Story 2 generate cleanly.

In Linear the poster gates are the goal, so the count that matters is the
highest gate rather than a separate goal number.

FILLER AND TRAPS

Filler fills whatever locations the progression and useful items do not. Each
type has an Off/Low/Medium/High weight, and traps replace a percentage of the
filler rather than being added on top -- so raising the trap percentage never
changes the total.
"""


# Level unlocks, Open mode only. Linear lets the game do its own locking.
LEVEL_UNLOCKS = [f"{name} Unlock" for _, name in D.LEVELS]
BOSS_UNLOCKS = [
    "Elephant Pong Unlock", "Gladiatoons Unlock", "Dodge City Unlock",
    "Disco Volcano Unlock", "The Hindenbird Unlock",
]

# Costumes and bonus games are shuffled in both modes.
COSTUMES = [
    "Skater", "Snowboarder", "Surfer", "Ninja", "DJ", "SWAT Officer",
    "Cowboy", "Werewolf", "Adventurer", "Caveman",
    # The hub booth. Its keeper is a check like any other, and this is the only
    # thing gating it in either mode.
    "Christmas Reindeer",
]
BONUS_UNLOCKS = [f"{name} Bonus Game Unlock" for lid, name in D.LEVELS
                 if lid not in D.NO_BONUS]

WANTED_POSTER = "Wanted Poster"
HINDENBIRD_TICKET = "Hindenbird Ticket"

# Weights, as the options describe them.
WEIGHT = {"off": 0, "low": 1, "medium": 3, "high": 6}

FILLER = {
    "Raised Bounty": "raised_bounty",
    "Chili Pepper": "chili_pepper",
    "Burp Can": "burp_can",
    "Invisibility": "invisibility",
    "Bubble Gum": "bubble_gum",
}
TRAPS = {
    "Dynamite Trap": "dynamite",
    "Squash Trap": "squash",
    "Electrocute Trap": "electrocute",
    "Hiccup Trap": "hiccup",
    "No Spinning Trap": "no_spinning",
    "Costume Strip Trap": "costume_strip",
}

# Never let a seed end up with nothing to fill with.
FALLBACK_FILLER = "Raised Bounty"


def _weighted(counts, weights, total):
    """Hand out `total` items in proportion to the weights."""
    live = {k: WEIGHT[weights.get(v, "low")] for k, v in counts.items()}
    live = {k: w for k, w in live.items() if w > 0}
    if not live or total <= 0:
        return {}
    pool = sum(live.values())
    out = {}
    given = 0
    for i, (name, w) in enumerate(sorted(live.items())):
        n = total * w // pool
        out[name] = n
        given += n
    # Rounding leaves a few over; give them to the heaviest.
    if given < total:
        heaviest = max(live, key=lambda k: (live[k], k))
        out[heaviest] += total - given
    return out


def progression_items(mode, options):
    """Items the generator must treat as gating something."""
    out = []

    if mode == "open":
        out += LEVEL_UNLOCKS
        # The Hindenbird's unlock is in the pool ONLY when the player made it
        # part of their goal. Otherwise there is no such item in the seed at
        # all: what opens the last fight is the goal being met, and putting an
        # unlock in the pool as well would mean a second, invisible condition
        # nobody asked for.
        hb = BOSS_UNLOCK[HINDENBIRD_LEVEL]
        out += [b for b in BOSS_UNLOCKS
                if b != hb or options.get("unlock_in_goal")]
    # Linear has no level unlocks: the game handles its own progression and
    # the bosses are gated on poster counts instead.

    out += COSTUMES
    out += BONUS_UNLOCKS

    # Only as many posters as the goal actually needs. The rest are useful,
    # which keeps them shuffled and worth finding without making the generator
    # treat each one as a potential gate.
    need = poster_requirement(mode, options)
    out += [WANTED_POSTER] * need

    # Hindenbird Tickets are deliberately NOT here. Each boss's second check
    # holds one, placed and locked in create_regions, so beating a boss is
    # what hands one over -- which is what the option text has always
    # promised. Shuffling them into the pool as well meant the ticket could
    # land anywhere in the multiworld while the boss check that is supposed to
    # BE the ticket handed out something else entirely.

    return out


def useful_items(mode, options):
    """Worth having, but not gating anything.

    Nothing at all when the requirement is zero. A poster that gates nothing
    and counts toward nothing is not useful -- it is filler wearing a
    progression item's name, and seventy of them is more than half a default
    Open seed. normalise already zeroes the pool for that case; this reaches
    the same answer on its own, so a caller that assembles an options dict by
    hand cannot put them back.
    """
    need = poster_requirement(mode, options)
    if need <= 0:
        return []
    pool = int(options.get("poster_pool", 70))
    return [WANTED_POSTER] * max(0, pool - need)


def poster_requirement(mode, options):
    """How many posters are genuinely required.

    Open uses the goal, and only when posters are part of it. Linear uses the
    largest boss gate, since passing that implies passing the earlier ones.
    """
    if mode == "linear":
        gates = [int(options.get(k, 0)) for k in
                 ("gate_elephant_pong", "gate_gladiatoons",
                  "gate_dodge_city", "gate_disco_volcano")]
        need = max(gates) if gates else 0
    else:
        need = (int(options.get("goal_posters", 50))
                if options.get("posters_in_goal") else 0)
    return min(need, int(options.get("poster_pool", 70)))


def filler_items(count, options):
    """Filler and traps for the locations progression does not fill.

    Traps replace filler rather than adding to it, so the trap percentage
    changes the mix and never the total.
    """
    if count <= 0:
        return []
    pct = max(0, min(100, int(options.get("trap_percent", 0))))
    n_traps = count * pct // 100
    n_filler = count - n_traps

    out = []
    for name, n in _weighted(FILLER, options, n_filler).items():
        out += [name] * n
    for name, n in _weighted(TRAPS, options, n_traps).items():
        out += [name] * n

    # Weights can all be Off, or rounding can leave a shortfall.
    while len(out) < count:
        out.append(FALLBACK_FILLER)
    return out[:count]


def build_pool(mode, options, location_count):
    """The full item list for a seed, sized to its locations."""
    prog = progression_items(mode, options)
    useful = useful_items(mode, options)
    fixed = len(prog) + len(useful)
    filler = filler_items(location_count - fixed, options)
    return {
        "progression": prog,
        "useful": useful,
        "filler": filler,
        "total": fixed + len(filler),
    }

# ==========================================================================
# OPTIONS
# ==========================================================================

#!/usr/bin/env python3
"""
taz_options.py -- the yaml options, and turning them into what the other
modules consume.

Everything here matches the options document. The point of the module is that
taz_data, taz_items and taz_rules all take a plain dict, so this is the single
place that knows about yaml names, defaults and the awkward interactions
between settings.

THE AWKWARD ONES

  Starting Destruction above what the difficulty allows drops one step, as the
  option text describes -- 75% on Standard becomes 25%, not 50%, because 50%
  is the goal itself and starting there would mean the check was already met.

  Goal Conditions is every combination of three things, so it is stored as a
  set of flags rather than one enum. A goal with nothing selected falls back to
  needing the Hindenbird unlock, or the seed would have no goal at all.

  Wanted Posters Goal above the pool is impossible, so it is clamped -- a
  player asking for 100 from a pool of 70 gets 70.
"""


GAME_MODES = ("open", "linear")
DIFFICULTIES = ("standard", "advanced", "expert")
WEIGHTS = ("off", "low", "medium", "high")
IN_GAME_TEXT_VALUES = ("off", "progressive", "all")

SANDWICH_CHECK_VALUES = (0, 1, 5, 10, 25, 50, 100)
SANDWICH_START_VALUES = (0, 25, 50, 75)
DESTRUCTION_CHECK_VALUES = (0, 1, 5, 10, 25, 50, 75, 100)

DEATH_LINK_SOURCES = ("captures", "void_out", "both")

# Goal Conditions, as ONE choice rather than three toggles.
#
# Three independent switches made "no goal at all" a state a player could
# reach, and a seed with nothing to require is not a seed -- it finishes
# itself. normalise used to catch that and quietly turn the level unlock on,
# which is a fix for a yaml that should never have been expressible.
#
# The numbering matches the badge stages the tracker already uses, so its
# option images line up without being renumbered:
#     posters, bosses, unlock, then the pairs, then all three.
GOAL_COMBOS = {
    0: (True,  False, False),
    1: (False, True,  False),
    2: (False, False, True),
    3: (True,  True,  False),
    4: (True,  False, True),
    5: (False, True,  True),
    6: (True,  True,  True),
}

# What sends a Death Link, in the same shape and for the same reason. Turning
# Death Link on and sending nothing is not a mode, so there is no empty
# combination to pick: choosing to participate means choosing what counts.
# Order is (captures, void outs, boss losses).
DEATH_LINK_COMBOS = {
    0: (True,  False, False),
    1: (False, True,  False),
    2: (False, False, True),
    3: (True,  True,  False),
    4: (True,  False, True),
    5: (False, True,  True),
    6: (True,  True,  True),
}

DEFAULTS = {
    # Game Mode
    "game_mode": "open",

    # Open Mode
    "starting_levels": 1,               # 0-5
    "goal_conditions": 0,               # an index into GOAL_COMBOS
    "poster_pool_open": 70,             # 10-100
    "goal_posters": 50,                 # 10-100
    "goal_bosses": 4,                   # 1-4

    # Linear Mode
    "poster_pool_linear": 70,           # 10-100
    "gate_elephant_pong": 21,           # 1-100
    "gate_gladiatoons": 42,             # 2-100
    "gate_dodge_city": 63,              # 3-100
    "gate_disco_volcano": 70,           # 4-100

    # Sandwiches
    "starting_sandwiches": 0,
    "sandwich_checks": 100,

    # Daffy-culty
    "difficulty": "standard",

    # Destruction
    "destruction_checks": 50,

    # Filler weights
    "raised_bounty": "high",
    "chili_pepper": "low",
    "burp_can": "low",
    "invisibility": "low",
    "bubble_gum": "low",

    # Traps
    "trap_percent": 0,                  # 0-100
    "dynamite": "low",
    "squash": "low",
    "electrocute": "low",
    "hiccup": "low",
    "no_spinning": "low",
    "costume_strip": "low",

    # Quality of Life
    # What the in-game subtitle box announces. "progressive" is the
    # Archipelago item classification, not items named "Progressive".
    "in_game_text": "progressive",
    # Keep this slot's own filler at home rather than posting it out. On by
    # default because a seed with every sandwich and every percent as a check
    # holds two thousand locations, and a slot that size sending all of its
    # filler into a multiworld swamps everyone else in it.
    "local_filler": True,

    # Death Link
    "death_link": False,
    "death_link_sends": 0,              # an index into DEATH_LINK_COMBOS
    "void_out_amnesty": 1,              # 1-5
}

RANGES = {
    "starting_levels": (0, 5),
    "poster_pool_open": (10, 100),
    "goal_posters": (10, 100),
    "goal_bosses": (1, 4),
    "poster_pool_linear": (10, 100),
    "gate_elephant_pong": (1, 100),
    "gate_gladiatoons": (2, 100),
    "gate_dodge_city": (3, 100),
    "gate_disco_volcano": (4, 100),
    "trap_percent": (0, 100),
    "void_out_amnesty": (1, 5),
}

CHOICES = {
    "game_mode": GAME_MODES,
    "difficulty": DIFFICULTIES,

    "starting_sandwiches": SANDWICH_START_VALUES,
    "goal_conditions": tuple(GOAL_COMBOS),
    "death_link_sends": tuple(DEATH_LINK_COMBOS),
    "sandwich_checks": SANDWICH_CHECK_VALUES,
    "destruction_checks": DESTRUCTION_CHECK_VALUES,
    "raised_bounty": WEIGHTS, "chili_pepper": WEIGHTS,
    "burp_can": WEIGHTS, "invisibility": WEIGHTS,
    "bubble_gum": WEIGHTS,
    "dynamite": WEIGHTS, "squash": WEIGHTS, "electrocute": WEIGHTS,
    "hiccup": WEIGHTS, "no_spinning": WEIGHTS,
    "costume_strip": WEIGHTS,

    "in_game_text": IN_GAME_TEXT_VALUES,
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class TazOptionError(Exception):
    """A yaml that asks for something impossible.

    Clamping quietly was the old behaviour, and it has a real cost: a player
    who asks for 75% starting destruction on Standard gets 25% and a warning
    they may never see, so the seed they play is not the seed they asked for.
    Refusing is louder, and the message says exactly what to change.

    Only raised during GENERATION. The client normalises the same dict from
    slot data, where the numbers have already been settled and refusing would
    strand a player mid-run.
    """


def normalise(raw=None, strict=False):
    """Fill in defaults, clamp everything, and resolve the interactions.

    Returns a flat dict the other modules can use without knowing any of the
    yaml rules.
    """
    o = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in o:
            o[k] = v
        elif k not in ("warnings",):
            # Keep anything else the caller sent. The slot data carries a few
            # things that are not options -- starting_levels_granted among
            # them -- and dropping them here left the client believing the
            # player owned no starting levels at all.
            o.setdefault(k, v)

    for k, (lo, hi) in RANGES.items():
        try:
            o[k] = _clamp(int(o[k]), lo, hi)
        except (TypeError, ValueError):
            o[k] = DEFAULTS[k]

    for k, allowed in CHOICES.items():
        if o[k] not in allowed:
            o[k] = DEFAULTS[k]

    # One choice in, three flags out. Everything downstream -- the rules, the
    # client, the tracker's slot data -- still reads the flags, so collapsing
    # the yaml into a dropdown changes what a player picks and nothing else.
    o["goal_posters_enabled"], o["goal_bosses_enabled"], \
        o["goal_unlock_enabled"] = GOAL_COMBOS[o["goal_conditions"]]
    o["death_link_captures"], o["death_link_void_outs"], \
        o["death_link_boss_losses"] = DEATH_LINK_COMBOS[o["death_link_sends"]]

    mode = o["game_mode"]
    warnings = []
    problems = []

    def bad(msg):
        """Refuse in strict mode, clamp otherwise.

        Generation is strict: a yaml that asks for the impossible should be
        told so, not quietly rebuilt into a different seed. The client is not,
        because by then the numbers are settled and refusing would strand a
        player mid-run.
        """
        (problems if strict else warnings).append(msg)

    # One pool, whichever mode is in play.
    o["poster_pool"] = (o["poster_pool_open"] if mode == "open"
                        else o["poster_pool_linear"])

    if mode == "open":
        # Open mode with no starting level has nothing reachable at all: every
        # location is inside a level, and every level needs an unlock the
        # player does not have. The fill then runs out of valid spots and dies
        # with an opaque "remaining locations are invalid".
        if int(o.get("starting_levels", 0)) < 1:
            bad("Starting Levels is 0. Open mode needs at least one level to "
                "begin in, or nothing is reachable and the seed cannot be "
                "filled.")
            o["starting_levels"] = 1

        # Every entry in GOAL_COMBOS requires something, so "no goal at all"
        # is no longer expressible and the fallback that used to live here is
        # gone with it. That was the point of collapsing the three toggles.
        if o["goal_posters_enabled"] and o["goal_posters"] > o["poster_pool"]:
            bad(f"Wanted Posters Goal ({o['goal_posters']}) is above the "
                f"Wanted Posters Pool ({o['poster_pool']}). Lower the goal, "
                f"or raise the pool.")
            o["goal_posters"] = o["poster_pool"]
    else:
        # Linear gates should climb; a later gate below an earlier one would
        # let a boss open before the one before it.
        gates = ["gate_elephant_pong", "gate_gladiatoons",
                 "gate_dodge_city", "gate_disco_volcano"]
        for i in range(1, len(gates)):
            if o[gates[i]] < o[gates[i - 1]]:
                bad(f"{gates[i]} ({o[gates[i]]}) is below {gates[i - 1]} "
                    f"({o[gates[i - 1]]}). The gates have to climb, or a "
                    f"later boss would open before an earlier one.")
                o[gates[i]] = o[gates[i - 1]]
        if o[gates[-1]] > o["poster_pool"]:
            bad(f"The last gate ({o[gates[-1]]}) is above the Wanted Posters "
                f"Pool ({o['poster_pool']}). Lower the gate, or raise the "
                f"pool.")
            o["poster_pool"] = o[gates[-1]]

    # Destruction thresholds stop at the Daffy-culty's target, so an interval
    # above it produces no checks whatsoever -- the range is empty. Silently
    # generating a seed with no destruction checks is not what anyone asking
    # for checks every 100% meant.
    dgoal = DESTRUCTION_GOAL[o["difficulty"]]
    if o["destruction_checks"] > dgoal:
        bad(f"Destruction Checks of {o['destruction_checks']}% cannot be "
            f"reached on {o['difficulty'].title()}, where the target is "
            f"{dgoal}%. Lower it, or raise the Daffy-culty.")
        o["destruction_checks"] = dgoal

    # Names the other modules expect.
    o["posters_in_goal"] = bool(o["goal_posters_enabled"])
    o["bosses_in_goal"] = bool(o["goal_bosses_enabled"])
    o["unlock_in_goal"] = bool(o["goal_unlock_enabled"])

    # Zero required has to mean zero in the pool.
    #
    # In Open with posters out of the goal a Wanted Poster gates nothing,
    # counts toward nothing and does nothing when it arrives -- the only rule
    # that reads them is _hindenbird_rule, behind posters_in_goal, and the
    # gate rules that read them live inside the LINEAR_ORDER loop. At the
    # default pool that is seventy inert items in a hundred and twenty seven
    # item seed, and they do not only sit in this world: they go out into
    # everybody else's, where they are just as useless.
    #
    # This is NOT the same as a pool ABOVE a goal, which is deliberate and
    # stays. Seventy in the pool for a goal of fifty means finding any fifty
    # of them, which fills more easily and keeps every one worth picking up.
    # The broken case is only requirement zero.
    #
    # No warning: the player did nothing wrong -- they chose a goal without
    # posters and left the pool at its default -- and summary() only mentions
    # the pool when posters are in the goal, so there is nothing to explain.
    if not poster_requirement(mode, o):
        o["poster_pool"] = 0

    # And the pool cannot be bigger than the seed.
    #
    # Progression and useful are fixed by the options; filler is only what is
    # left over. So if the fixed part alone is larger than the vacancies there
    # is nowhere to put the surplus and the fill dies with "no more locations"
    # -- which reads as the world being broken rather than as a yaml asking
    # for the impossible.
    #
    # Reachable today: Sandwich Checks and Destruction Checks both off leaves
    # 103 vacancies in Open, and the Wanted Posters goal alone brings 104
    # items. The poster pool is the only elastic part, so it is what gives.
    o["mode"] = mode                        # location_args wants it
    try:
        locs = all_locations(**location_args(o))
        room = len(locs) - sum(1 for l in locs if l.get("type") == "ticket")
        fixed = len(progression_items(mode, o)) + len(useful_items(mode, o))
    except Exception:                       # never block a client on this
        fixed = room = 0
    if fixed > room:
        bad(f"This seed needs {fixed} items to exist but has only {room} "
            f"places to put them. Turn some checks on -- Sandwich Checks and "
            f"Destruction Checks are much the biggest -- or lower the Wanted "
            f"Posters Pool.")
        keep = max(0, o["poster_pool"] - (fixed - room))
        o["poster_pool"] = keep
        if o["posters_in_goal"]:
            o["goal_posters"] = min(o["goal_posters"], keep)

    o["mode"] = mode
    o["warnings"] = warnings
    if problems:
        raise TazOptionError(
            "Taz Wanted cannot generate with these options:\n  - "
            + "\n  - ".join(problems))
    return o


def location_args(o):
    """The arguments taz_data.all_locations wants."""
    return {
        "sandwich_interval": o["sandwich_checks"],
        "destruction_interval": o["destruction_checks"],
        "difficulty": o["difficulty"],
        "start_sandwiches": o["starting_sandwiches"],
        "with_tickets": o["mode"] == "open" and o["bosses_in_goal"],
    }


def summary(o):
    lines = [f"  mode            {o['mode']}",
             f"  difficulty      {o['difficulty']} "
             f"(destruction goal {D.DESTRUCTION_GOAL[o['difficulty']]}%)"]
    if o["mode"] == "open":
        goal = [n for n, on in (("posters", o["posters_in_goal"]),
                                ("bosses", o["bosses_in_goal"]),
                                ("level unlock", o["unlock_in_goal"])) if on]
        lines.append(f"  goal            {', '.join(goal)}")
        if o["posters_in_goal"]:
            lines.append(f"  posters         {o['goal_posters']} of "
                         f"{o['poster_pool']}")
        if o["bosses_in_goal"]:
            lines.append(f"  bosses          {o['goal_bosses']}")
        lines.append(f"  starting levels {o['starting_levels']}")
    else:
        lines.append(f"  gates           "
                     f"{o['gate_elephant_pong']}, {o['gate_gladiatoons']}, "
                     f"{o['gate_dodge_city']}, {o['gate_disco_volcano']} "
                     f"of {o['poster_pool']}")
    lines.append(f"  sandwiches      every {o['sandwich_checks'] or 'never'}"
                 f", starting at {o['starting_sandwiches']}")
    lines.append(f"  destruction     every "
                 f"{o['destruction_checks'] or 'never'}%")
    lines.append(f"  traps           {o['trap_percent']}% of filler")
    lines.append(f"  death link      "
                 + (f"on, amnesty "
                    f"{o['void_out_amnesty']}" if o["death_link"] else "off"))
    return "\n".join(lines)

# ==========================================================================
# REGIONS AND RULES
# ==========================================================================

#!/usr/bin/env python3
"""
taz_rules.py -- the region graph and what it takes to reach each location.

The two modes gate completely differently, so the graph is built per mode
rather than shared with exceptions bolted on.

OPEN

  Every level is its own region, reachable as soon as its unlock is found. The
  hubs are always reachable -- the client keeps their access field set -- so
  there is no hub-to-hub progression to model.

  A boss needs its own unlock. The Hindenbird additionally needs the whole
  goal: posters, boss defeats, level unlock, or whatever combination the
  player chose. That is what keeps it shut until the run is actually over.

TAZLAND IS SPLIT

  Its bridge is geofenced, so part of the level is reachable before the
  Tazland unlock and the rest is not. Measured in game: on Standard, 9
  sandwiches sit before the bridge; on Advanced and Expert, none do. Only 2%
  destruction is reachable either way, which is below the smallest check
  interval, so no destruction check is ever in logic there.

LINEAR

  No level unlocks at all -- the game handles that. Levels are reachable in
  order, and each boss needs a number of Wanted Posters. Everything in a hub's
  three levels is reachable once the previous boss is beaten.
"""


# Which hub each level belongs to, and which boss that hub leads to.
HUB_OF = {4: 3, 5: 3, 6: 3,
          9: 8, 10: 8, 11: 8,
          14: 13, 15: 13, 16: 13,
          18: 18}
HUB_BOSS = {3: 7, 8: 12, 13: 17, 18: 19}

# Linear plays through in this order; each hub opens when the previous boss
# falls.
LINEAR_ORDER = [
    (3, [4, 5, 6], 7),
    (8, [9, 10, 11], 12),
    (13, [14, 15, 16], 17),
    (18, [18], 19),
]

BOSS_NAME = {lid: name for lid, name in D.BOSSES}
LEVEL_UNLOCK = {lid: f"{name} Unlock" for lid, name in D.LEVELS}
BOSS_UNLOCK = {7: "Elephant Pong Unlock", 12: "Gladiatoons Unlock",
               17: "Dodge City Unlock", 19: "Disco Volcano Unlock",
               20: "The Hindenbird Unlock"}

TAZLAND = 18
MENU = "Menu"


# ---------------------------------------------------------------- regions


# Hubs that hold a check of their own. They need a region because a location
# has to live somewhere, and they are always reachable -- the client keeps
# their access field set, so nothing gates walking into one. What gates the
# checks inside is the costume, which is a location rule rather than an
# entrance one.
CATCHER_HUBS = {3: "Yosemite Zoo", 8: "Sam Francisco", 13: "Wile E. West"}


def regions(mode):
    """Region name -> the regions it connects to.

    Tazland is two regions in Open because its bridge is geofenced: the near
    side is reachable without the unlock, the far side is not.
    """
    out = {MENU: []}
    for name in CATCHER_HUBS.values():
        out[name] = []
        out[MENU].append(name)
    for lid, name in D.LEVELS:
        if lid == TAZLAND and mode == "open":
            out[f"{name} (Entrance)"] = [f"{name}"]
            out[name] = []
            out[MENU].append(f"{name} (Entrance)")
            continue
        out[name] = []
        out[MENU].append(name)
    for lid, name in D.BOSSES:
        short = BOSS_UNLOCK[lid].replace(" Unlock", "")
        out[short] = []
        out[MENU].append(short)
    return out


def region_of(loc, mode, difficulty="standard", start_sandwiches=0):
    """Which region a location sits in.

    Only the Tazland split needs thought: on Standard the first 9 sandwiches
    are before the bridge, so they belong to the entrance region rather than
    the level proper.

    What counts as "before the bridge" depends on what the player STARTED
    with. A seed beginning at 25 sandwiches needs five more for the check at
    30, and nine sit before the bridge -- so that check is reachable without
    the Tazland unlock, and calling it gated put it out of logic for a player
    who could walk to it.
    """
    if loc["type"] in ("boss", "ticket"):
        return BOSS_UNLOCK[loc["level"]].replace(" Unlock", "")

    # A hub's own check, which lives in the hub rather than in any level.
    if loc["level"] in CATCHER_HUBS:
        return CATCHER_HUBS[loc["level"]]

    # A bonus game is played at a booth OUTSIDE its level, so in Open mode it
    # belongs to the hub rather than to the level -- putting it in the level
    # would gate it on an unlock the player does not need.
    if loc["type"] == "bonus" and mode == "open":
        hub = {4: 3, 5: 3, 6: 3, 9: 8, 10: 8, 11: 8,
               14: 13, 15: 13, 16: 13}.get(loc["level"])
        if hub in CATCHER_HUBS:
            return CATCHER_HUBS[hub]

    name = D.LEVEL_NAME[loc["level"]]
    if loc["level"] != TAZLAND or mode != "open":
        return name

    pre = D.TAZLAND_PREGATE[difficulty]
    if loc["type"] == "sandwich" and \
            loc["threshold"] <= start_sandwiches + pre["sandwiches"]:
        return f"{name} (Entrance)"
    if loc["type"] == "destruction" and loc["threshold"] <= pre["destruction"]:
        return f"{name} (Entrance)"
    return name


# ---------------------------------------------------------------- rules


def _has(state, item, n=1):
    """Placeholder for the AP CollectionState call, so this module can be
    tested without importing Archipelago."""
    return state.get(item, 0) >= n


def entrance_rules(mode, options):
    """region -> a rule callable taking a dict of item -> count."""
    rules = {}

    if mode == "open":
        for lid, name in D.LEVELS:
            item = LEVEL_UNLOCK[lid]
            if lid == TAZLAND:
                # The entrance needs nothing; the level proper needs the
                # unlock, because the bridge is what the geofence blocks.
                rules[f"{name} (Entrance)"] = lambda s: True
                rules[name] = (lambda it: lambda s: _has(s, it))(item)
            else:
                rules[name] = (lambda it: lambda s: _has(s, it))(item)

        for lid, _ in D.BOSSES:
            short = BOSS_UNLOCK[lid].replace(" Unlock", "")
            if lid == 20:
                # The goal, and only the goal. In Open the last fight is not
                # something an item hands over -- it is what the player set
                # out to qualify for, so whatever they chose is what opens it.
                # When they chose the unlock, the unlock is in the pool and is
                # that condition; when they did not, it is not in the pool at
                # all and the posters or the boss defeats are.
                #
                # This used to gate on the unlock item unconditionally, to
                # avoid a circle: the goal counts Hindenbird Tickets, and the
                # Hindenbird's own ticket would have been a reward for beating
                # it. That circle is gone -- boss_locations leaves the
                # Hindenbird out entirely, so the four tickets all come from
                # fights that do not depend on this one.
                rules[short] = _hindenbird_rule(options)
            else:
                rules[short] = (lambda it: lambda s: _has(s, it))(
                    BOSS_UNLOCK[lid])
        return rules

    # Linear: a hub opens on TWO things -- the poster gate, and the boss
    # before it being beaten. Requiring only the gate was too generous by one
    # boss, which is why the tracker kept opening the last hub a step early.
    #
    # "Beaten" is an event placed in each boss's region, because there is no
    # item for it: a boss defeat is a check, and a check cannot gate anything.
    prev_gate = 0
    prev_boss = None
    for hub, levels, boss in LINEAR_ORDER:
        gate_here = prev_gate
        needs = prev_boss

        for lid in levels:
            name = D.LEVEL_NAME.get(lid)
            if not name:
                continue
            rules[name] = (lambda n, b: lambda s:
                           (n == 0 or _has(s, "Wanted Poster", n))
                           and (b is None or _has(s, b)))(gate_here, needs)

        short = BOSS_UNLOCK[boss].replace(" Unlock", "")
        need_boss = _linear_gate(boss, options)
        rules[short] = (lambda n, b: lambda s:
                        _has(s, "Wanted Poster", n)
                        and (b is None or _has(s, b)))(need_boss, needs)

        prev_gate = need_boss
        prev_boss = BEATEN_EVENT[boss]

    # The Hindenbird needs the last gate AND Disco Volcano beaten.
    rules["The Hindenbird"] = (lambda n: lambda s:
                               _has(s, "Wanted Poster", n)
                               and _has(s, BEATEN_EVENT[19])
                               )(_linear_gate(19, options))
    return rules


def _linear_gate(boss, options):
    return int(options.get({
        7: "gate_elephant_pong", 12: "gate_gladiatoons",
        17: "gate_dodge_city", 19: "gate_disco_volcano",
        20: "gate_disco_volcano",
    }[boss], 0))


def _hindenbird_rule(options):
    """The Hindenbird stays shut until the whole goal is met.

    Whichever combination the player picked -- posters, boss defeats, the level
    unlock, or several of them -- all of it has to be satisfied. This is the
    rule that makes the goal mean something rather than being a formality.
    """
    want_posters = bool(options.get("posters_in_goal"))
    want_bosses = bool(options.get("bosses_in_goal"))
    want_unlock = bool(options.get("unlock_in_goal"))
    n_posters = int(options.get("goal_posters", 50))
    n_bosses = int(options.get("goal_bosses", 4))

    def rule(s):
        if want_posters and not _has(s, "Wanted Poster", n_posters):
            return False
        if want_bosses and not _has(s, "Hindenbird Ticket", n_bosses):
            return False
        if want_unlock and not _has(s, "The Hindenbird Unlock"):
            return False
        # A goal with nothing selected still needs the level itself.
        if not (want_posters or want_bosses or want_unlock):
            return _has(s, "The Hindenbird Unlock")
        return True

    return rule


# Which costume each level's phone booth gives. Defeating a keeper costs Taz
# the costume he is wearing, so a catcher cannot be beaten without one -- the
# booth has to be usable, which means the costume item has to have arrived.
LEVEL_COSTUME_NAME = {
    3: "Christmas Reindeer",      # the hub booth
    10: "Ninja", 16: "Cowboy", 6: "Surfer", 9: "DJ", 14: "Werewolf",
    15: "Adventurer", 18: "Caveman", 4: "Snowboarder", 11: "SWAT Officer",
    5: "Skater",
}


def location_rules(mode, options, difficulty="standard", catchers=None):
    """Extra rules beyond simply reaching the region.

    Bonus games need their unlock. Catchers need the level's costume: beating
    one costs Taz whatever he is wearing, so without the costume the fight
    cannot be had at all.
    """
    out = {}
    for lid, name in D.LEVELS:
        if lid not in D.NO_BONUS:
            item = f"{name} Bonus Game Unlock"
            out[f"{name} - Bonus Game Completed"] = (
                lambda it: lambda s: _has(s, it))(item)
            # The booth is outside the level entrance, so in Open mode the
            # level's own unlock is not needed to play it. The region rule
            # below would otherwise require it, and a seed could put a check
            # behind a level the player has no way in to.

    for loc in D.catcher_locations(catchers or {}):
        costume = LEVEL_COSTUME_NAME.get(loc["level"])
        # Only gate on a costume that is actually in the pool. The hub's booth
        # gives the Christmas Reindeer, which is not an item because the hub's
        # catcher is not a location -- referencing it would make that check
        # unreachable rather than merely ungated.
        if costume and costume in COSTUMES:
            out[loc["name"]] = (lambda it: lambda s: _has(s, it))(costume)
    return out


def goal_rule(mode, options):
    """What finishing the seed means.

    In Linear that is reaching The Hindenbird, which is the last poster gate --
    the same condition as its entrance.
    """
    if mode == "linear":
        return (lambda n: lambda s:
                _has(s, "Wanted Poster", n))(_linear_gate(19, options))
    return _hindenbird_rule(options)
