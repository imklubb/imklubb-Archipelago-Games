"""
Options.py -- the yaml options, as Archipelago option classes.

The docstrings are the player-facing notes, word for word from the options
document: they are what appears on the website and in generated templates, so
they say what the option does rather than how it is implemented.

The behaviour behind them lives in logic.py -- defaults, ranges, and the
awkward interactions between settings. This file is the thin translation layer,
so an option changes in one place rather than two that can drift apart.
"""

from dataclasses import dataclass
from typing import Any, Dict

from Options import (Choice, DeathLink, DefaultOnToggle,
                     PerGameCommonOptions, Range, Toggle)

# OptionGroup is also newer. Without it the options still work, they just are
# not sorted into categories on the website.
try:
    from Options import OptionGroup
except ImportError:
    OptionGroup = None

from . import logic as O


# --- Game Mode --------------------------------------------------------------

class GameMode(Choice):
    """How would you like to experience Taz Wanted?

    Open Mode has all of the levels at your disposal at the very start. Find
    the level unlocks to gain access to them!

    Linear Mode has you playing the game in order. In this mode you have to
    fight every boss.
    """
    display_name = "Game Mode"
    option_open = 0
    option_linear = 1
    default = 0


# --- Open Mode --------------------------------------------------------------

class StartingLevels(Range):
    """NOTE: This does nothing if you didn't select Open Mode.

    How many levels would you like to start your game with?

    At least one: with none, every location is inside a level you cannot
    enter, so there is nowhere for the generator to place anything.
    """
    display_name = "Starting Levels"
    range_start = 1
    range_end = 5
    default = 1


class GoalConditions(Choice):
    """NOTE: This does nothing if you didn't select Open Mode.

    What you have to do to open The Hindenbird and win.

    This is one setting rather than three switches because a goal has to
    require SOMETHING -- with separate toggles it was possible to turn all
    three off and generate a seed that was already finished.

    Wanted Posters uses your Wanted Posters Goal. Defeated Bosses uses your
    Defeated Bosses Goal, and gives every boss a second check holding a
    Hindenbird Ticket. Level Unlock puts The Hindenbird Unlock in the item
    pool -- and if you do NOT pick it, that item does not exist in the seed at
    all, because then the goal itself is the only thing standing between you
    and the last fight.
    """
    display_name = "Goal Conditions"
    option_wanted_posters = 0
    option_defeated_bosses = 1
    option_level_unlock = 2
    option_posters_and_bosses = 3
    option_posters_and_unlock = 4
    option_bosses_and_unlock = 5
    option_all_three = 6
    default = 0


class WantedPostersPoolOpen(Range):
    """NOTE: This does nothing if you didn't select Open Mode, or if Wanted
    Posters are not part of your Goal Conditions.

    Determines how many Wanted Posters are in the item Pool. Setting this
    above your Wanted Posters Goal is fine, and often better -- a pool of 70
    for a goal of 50 means finding any 50 of them, anywhere.

    If your goal does not include Wanted Posters then nothing in the seed
    requires one, so none are added at all and those slots become filler
    instead.
    """
    display_name = "Wanted Posters Pool - Open"
    range_start = 10
    range_end = 100
    default = 70


class WantedPostersGoal(Range):
    """NOTE: This does nothing if you didn't select Open Mode.

    Determines how many Wanted Posters are needed to reach your goal.
    """
    display_name = "Wanted Posters Goal"
    range_start = 10
    range_end = 100
    default = 50


class DefeatedBosses(Range):
    """NOTE: This does nothing if you didn't select it as your game mode or
    goal condition.

    Determines how many bosses you have to defeat to reach your goal.
    """
    display_name = "Defeated Bosses"
    range_start = 1
    range_end = 4
    default = 4


# --- Linear Mode ------------------------------------------------------------

class WantedPostersPoolLinear(Range):
    """NOTE: This does nothing if you didn't select Linear Mode.

    Determines how many Wanted Posters are in the item Pool.
    """
    display_name = "Wanted Posters Pool - Linear"
    range_start = 10
    range_end = 100
    default = 70


class GateElephantPong(Range):
    """NOTE: This does nothing if you didn't select Linear Mode.

    Determines how many Wanted Posters are needed to enter Elephant Pong.
    """
    display_name = "Elephant Pong Wanted Posters Gate"
    range_start = 1
    range_end = 100
    default = 21


class GateGladiatoons(Range):
    """NOTE: This does nothing if you didn't select Linear Mode.

    Determines how many Wanted Posters are needed to enter Gladiatoons.
    """
    display_name = "Gladiatoons Wanted Posters Gate"
    range_start = 2
    range_end = 100
    default = 42


class GateDodgeCity(Range):
    """NOTE: This does nothing if you didn't select Linear Mode.

    Determines how many Wanted Posters are needed to enter Dodge City.
    """
    display_name = "Dodge City Wanted Posters Gate"
    range_start = 3
    range_end = 100
    default = 63


class GateDiscoVolcano(Range):
    """NOTE: This does nothing if you didn't select Linear Mode.

    Determines how many Wanted Posters are needed to enter Disco Volcano which
    leads into The Hindenbird.
    """
    display_name = "Disco Volcano & Hindenbird Wanted Posters Gate"
    range_start = 4
    range_end = 100
    default = 70


# --- Sandwiches -------------------------------------------------------------

class StartingSandwiches(Choice):
    """Collecting 100 Sandwiches sends a check. This gets you there sooner."""
    display_name = "Starting Sandwiches"
    option_0 = 0
    option_25 = 25
    option_50 = 50
    option_75 = 75
    default = 0


class SandwichChecks(Choice):
    """By default, you'll receive an item for getting 100 sandwiches. This
    allows you to get more checks based on which value you set it to. For
    example, if you start with 0 and set your Checks to 5, each level will have
    20 sandwich checks in it. If you set it to 0, no checks will be sent for
    sandwiches.

    1 makes every single sandwich a check: 100 per level, 1000 in the seed.
    The tracker shows those as one counter per level rather than a thousand
    separate pins.
    """
    display_name = "Sandwich Checks"
    option_0 = 0
    option_1 = 1
    option_5 = 5
    option_10 = 10
    option_25 = 25
    option_50 = 50
    option_100 = 100
    default = 100


# --- Daffy-culty ------------------------------------------------------------

class Difficulty(Choice):
    """YOU MUST SELECT THIS IN GAME BEFORE STARTING!

    Changing this makes various things in the game harder, but for AP checks,
    it determines when a Destruction Bonus should trigger.

    Standard=50%  Advanced=75%  Expert=100%
    """
    display_name = "Starting Daffy-culty"
    option_standard = 0
    option_advanced = 1
    option_expert = 2
    default = 0


# --- Destruction ------------------------------------------------------------

# Starting Destruction used to live here. It was removed rather than fixed:
# the percentage a level shows is not a number the game stores, it is derived
# from how much of the level is still standing, so there is nothing to write.
# Seeding the save field only moved the "best ever" figure while the meter
# still began every run at zero -- an option that could not do what it said.
class DestructionChecks(Choice):
    """How often destroying a level sends a check, as a percentage of it.

    Your Daffy-culty sets the target: 50% on Standard, 75% on Advanced, 100%
    on Expert. A check fires at every multiple of this value up to that
    target, and the target itself ALWAYS pays out -- so when the value does
    not divide the target evenly, the remainder is a check of its own.

    On Expert, checks every 75% is two checks: one at 75% and one at 100%.
    On Advanced, checks every 50% is two: 50% and 75%. On Standard, checks
    every 5% is ten, 5% through 50%.

    0% means no destruction checks at all. 1% makes every whole percent a
    check -- up to 100 per level on Expert -- which the tracker shows as one
    counter per level rather than as a hundred pins.

    75% needs Advanced or Expert and 100% needs Expert, since a value above
    the target could never be reached. Choosing one the Daffy-culty cannot
    reach refuses to generate, rather than quietly building a seed with no
    destruction checks in it.
    """
    display_name = "Destruction Checks"
    option_0 = 0
    option_1 = 1
    option_5 = 5
    option_10 = 10
    option_25 = 25
    option_50 = 50
    option_75 = 75
    option_100 = 100
    default = 50

    @classmethod
    def get_option_name(cls, value: int) -> str:
        """Display only -- these are percentages and should read as such.

        The yaml still takes the bare number: acceptance is decided by the
        option_* names, and this only changes what is shown.
        """
        return f"{value}%"


# --- Filler -----------------------------------------------------------------

class _Weight(Choice):
    """Off, Low, Medium or High. Off removes the item from the pool entirely.

    If every filler and trap weight is Off, Raised Bounty is set to High so
    there is still something to fill the remaining locations with.
    """
    option_off = 0
    # `alias_` rather than a second `option_`: Archipelago refuses two options
    # sharing an ID, and an alias is how a synonym is meant to be declared.
    alias_none = 0
    option_low = 1
    option_medium = 2
    option_high = 3
    default = 1


class RaisedBounty(_Weight):
    """Does Nothing, but Makes Sam mad!"""
    display_name = "Raised Bounty Filler Weight"
    default = 3


class ChiliPepper(_Weight):
    """HOT HOT HOT!"""
    display_name = "Chili Pepper Filler Weight"


class BurpCan(_Weight):
    """That tasted great!"""
    display_name = "Burp Can Filler Weight"


class Invisibility(_Weight):
    """Now you see me, now you don't!"""
    display_name = "Invisibility Filler Weight"


class BubbleGum(_Weight):
    """Sticky..."""
    display_name = "Bubble Gum Filler Weight"


# --- Traps ------------------------------------------------------------------

class TrapPercent(Range):
    """Determines the percentage of filler items that will be replaced with
    traps.
    """
    display_name = "Filler Replaced With Traps"
    range_start = 0
    range_end = 100
    default = 0


class DynamiteTrap(_Weight):
    """Taz no feel so good..."""
    display_name = "Dynamite Trap Weight"


class SquashTrap(_Weight):
    """Like a pancake..."""
    display_name = "Squash Trap Weight"


class ElectrocuteTrap(_Weight):
    """Shocking... Isn't it..."""
    display_name = "Electrocute Trap Weight"


class HiccupTrap(_Weight):
    """That caught me off guard!"""
    display_name = "Hiccup Trap Weight"


class NoSpinningTrap(_Weight):
    """Finally, I was getting Dizzy"""
    display_name = "No Spinning Trap Weight"


class CostumeStripTrap(_Weight):
    """hey! I was wearing that!"""
    display_name = "Costume Strip Trap Weight"


# --- Quality of Life --------------------------------------------------------

class LocalFiller(DefaultOnToggle):
    """Keep your own filler items in your own game instead of sending them out.

    Every sandwich or every percent of destruction as a check means up to two
    thousand locations in one slot, and almost all of them hold filler. Sent
    into a multiworld that is a flood: everyone else spends the seed opening
    your Chili Peppers.

    With this on, your filler is placed in your own world before the fill
    begins, so you find it yourself. Traps are not affected and still travel
    normally -- a trap that cannot reach anyone else is not a trap.

    Some of it still goes out. Enough of your locations are deliberately left
    free for progression to have somewhere to land, and on a small seed -- no
    sandwich or destruction checks -- there is no spare filler to keep, so
    this does nothing at all.
    """
    display_name = "Local Filler"


class InGameTextClient(Choice):
    """Announce received items in the game's own subtitle box.

    Off shows nothing. Progression Only shows items Archipelago has
    classified as progression -- posters, costumes, unlocks, tickets -- and
    stays quiet for filler and traps. All Items shows everything.

    Messages wait until the player actually has control, so nothing appears
    over a loading screen, a cutscene or the pause menu; they queue and
    arrive once Taz is playable again.

    The player can also cycle this in game by holding both bumpers and both
    triggers together. That change lasts for the session and does not alter
    the yaml.
    """
    display_name = "In Game Text Client"
    option_off = 0
    option_progressive = 1
    option_all = 2
    alias_progression = 1
    default = 1


class TazDeathLink(DeathLink):
    """If Taz dies you kill everyone else that has Death Link on and vice
    versa. The way it works in Taz Wanted, is Taz is teleported to the start
    of the level, unless he's in a boss fight, then he just loses.
    """
    display_name = "Death Link"


class DeathLinkSends(Choice):
    """Which of your deaths kill everyone else.

    NOTE: Does nothing if Death Link is turned off.

    Captures is a catcher grabbing you. Void Outs is dying any other ordinary
    way -- drowning, falling, being crushed, or wrecking a rollercoaster.
    Boss Losses is losing a boss fight.

    There is no "nothing" here on purpose: turning Death Link on and sending
    none of your own deaths is not a way to play, it is a setting somebody
    forgot to finish. If you want to be left alone, turn Death Link off.
    """
    display_name = "What Sends a Death Link"
    option_captures = 0
    option_void_outs = 1
    option_boss_losses = 2
    option_captures_and_void_outs = 3
    option_captures_and_boss_losses = 4
    option_void_outs_and_boss_losses = 5
    option_all_three = 6
    default = 0


class VoidOutAmnesty(Range):
    """How many Void Out deaths before a death link is sent. This setting does
    nothing if void out deaths weren't selected.
    """
    display_name = "Void Out Amnesty"
    range_start = 1
    range_end = 5
    default = 1


@dataclass
class TazOptions(PerGameCommonOptions):
    game_mode: GameMode

    starting_levels: StartingLevels
    goal_conditions: GoalConditions
    poster_pool_open: WantedPostersPoolOpen
    goal_posters: WantedPostersGoal
    goal_bosses: DefeatedBosses

    poster_pool_linear: WantedPostersPoolLinear
    gate_elephant_pong: GateElephantPong
    gate_gladiatoons: GateGladiatoons
    gate_dodge_city: GateDodgeCity
    gate_disco_volcano: GateDiscoVolcano

    starting_sandwiches: StartingSandwiches
    sandwich_checks: SandwichChecks

    difficulty: Difficulty

    destruction_checks: DestructionChecks

    raised_bounty: RaisedBounty
    chili_pepper: ChiliPepper
    burp_can: BurpCan
    invisibility: Invisibility
    bubble_gum: BubbleGum

    trap_percent: TrapPercent
    dynamite: DynamiteTrap
    squash: SquashTrap
    electrocute: ElectrocuteTrap
    hiccup: HiccupTrap
    no_spinning: NoSpinningTrap
    costume_strip: CostumeStripTrap

    in_game_text: InGameTextClient
    local_filler: LocalFiller

    death_link: TazDeathLink
    death_link_sends: DeathLinkSends
    void_out_amnesty: VoidOutAmnesty


# The website groups options under these headings, in this order. Without them
# everything lands in one undifferentiated list.
def _groups():
    if OptionGroup is None:
        return []
    return [
    OptionGroup("Game Mode", [GameMode]),
    OptionGroup("Open Mode", [
        StartingLevels, GoalConditions, WantedPostersPoolOpen,
        WantedPostersGoal, DefeatedBosses,
    ]),
    OptionGroup("Linear Mode", [
        WantedPostersPoolLinear, GateElephantPong, GateGladiatoons,
        GateDodgeCity, GateDiscoVolcano,
    ]),
    OptionGroup("Sandwiches", [StartingSandwiches, SandwichChecks]),
    OptionGroup("Daffy-culty", [Difficulty]),
    OptionGroup("Destruction", [DestructionChecks]),
    OptionGroup("Filler", [
        RaisedBounty, ChiliPepper, BurpCan, Invisibility, BubbleGum,
    ]),
    OptionGroup("Traps", [
        TrapPercent, DynamiteTrap, SquashTrap, ElectrocuteTrap,
        HiccupTrap, NoSpinningTrap, CostumeStripTrap,
    ]),
    OptionGroup("Quality of Life", [InGameTextClient, LocalFiller]),
    OptionGroup("Death Link", [
        TazDeathLink, DeathLinkSends, VoidOutAmnesty,
    ]),
    ]


option_groups = _groups()


# Choice options carry an integer; the logic works in the names it documents,
# so they are translated here rather than there.
_NAMED = {
    "game_mode": {0: "open", 1: "linear"},
    "difficulty": {0: "standard", 1: "advanced", 2: "expert"},

    "raised_bounty": {0: "off", 1: "low", 2: "medium", 3: "high"},
    "in_game_text": {0: "off", 1: "progressive", 2: "all"},
}
for _k in ("chili_pepper", "burp_can", "invisibility", "bubble_gum",
           "dynamite", "squash",
           "electrocute", "hiccup", "no_spinning", "costume_strip"):
    _NAMED[_k] = _NAMED["raised_bounty"]

_WEIGHTS = ("raised_bounty", "chili_pepper", "burp_can", "invisibility",
            "bubble_gum", "dynamite", "squash", "electrocute", "hiccup",
            "no_spinning", "costume_strip")


def to_dict(options: TazOptions) -> Dict[str, Any]:
    """Turn the option objects into the plain dict the logic takes."""
    raw = {}
    for name in O.DEFAULTS:
        opt = getattr(options, name, None)
        if opt is None:
            continue
        value = opt.value
        if name in _NAMED:
            value = _NAMED[name].get(value, O.DEFAULTS[name])
        elif isinstance(O.DEFAULTS.get(name), bool):
            # Send a real boolean for anything that IS one. A Toggle's value
            # is 0 or 1, and both are truthy in Lua -- so the tracker could
            # not tell an option that was off from one that was on, and read
            # whichever way the JSON happened to encode it.
            value = bool(value)
        raw[name] = value

    # Every weight Off would leave nothing to fill the remaining locations
    # with, so Raised Bounty comes back at High. It is the filler that does
    # nothing the player can feel, which makes it the safe default.
    if all(raw.get(k, "off") == "off" for k in _WEIGHTS):
        raw["raised_bounty"] = "high"

    return O.normalise(raw, strict=True)


def slot_data(options: TazOptions) -> Dict[str, Any]:
    """What the client is told about the seed.

    It gets the normalised dict, so the client never repeats the clamping and
    the two cannot disagree about what the seed actually is.
    """
    d = to_dict(options)
    d.pop("warnings", None)
    return d
