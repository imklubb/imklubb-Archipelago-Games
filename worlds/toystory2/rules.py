from typing import TYPE_CHECKING, Callable, List, Optional
import re
from BaseClasses import CollectionState
from .logic_data import ALL_LOCATIONS, COIN_DATA, LOC_BY_NAME, Loc
from .items import TOY_BUNDLE_NAME

if TYPE_CHECKING:
    from . import ToyStory2World

# ============================================================
# CONSTANTS
# ============================================================
SKIPS_OFF  = 0
SKIPS_EASY = 1
SKIPS_HARD = 2
SKIPS_INSANE = 3

GAME_MODE_OPEN   = 0
GAME_MODE_LINEAR = 1

GOAL_TOKENS   = 0
GOAL_BOSSES   = 1
GOAL_UNLOCK   = 2
GOAL_T_AND_B  = 3
GOAL_T_AND_U  = 4
GOAL_B_AND_U  = 5
GOAL_T_B_U    = 6

# ============================================================
# MOVE / GADGET HELPERS
# ============================================================

def has_laser(state: CollectionState, player: int) -> bool:
    return state.has("Progressive Laser", player)

def has_spin(state: CollectionState, player: int) -> bool:
    return state.has("Progressive Spin", player)

def has_stomp(state: CollectionState, player: int) -> bool:
    return state.has("Stomp", player)

def has_double_jump(state: CollectionState, player: int) -> bool:
    return state.has("Double Jump", player)

def has_pole_climb(state: CollectionState, player: int) -> bool:
    return state.has("Pole Climb", player)

def has_ledge_grab(state: CollectionState, player: int) -> bool:
    return state.has("Ledge Grab", player)

def has_pole_vault(state: CollectionState, player: int) -> bool:
    return state.has("Pole Vault", player)

def has_push(state: CollectionState, player: int) -> bool:
    return state.has("Push", player)

def has_rope_sliding(state: CollectionState, player: int) -> bool:
    return state.has("Rope Sliding", player)

def has_visor(state: CollectionState, player: int) -> bool:
    return state.has("Visor", player)

def has_any_attack(state: CollectionState, player: int) -> bool:
    return has_laser(state, player) or has_spin(state, player) or has_stomp(state, player)

def has_laser_or_spin(state: CollectionState, player: int) -> bool:
    return has_laser(state, player) or has_spin(state, player)

# Gadget helpers
def has_gadget(state: CollectionState, player: int, gadget_name: str) -> bool:
    return state.has(gadget_name, player)

def has_cosmic_shield_andys(state: CollectionState, player: int) -> bool:
    return state.has("Cosmic Shield - Andy's House", player)

def has_rocket_boots_neighborhood(state: CollectionState, player: int) -> bool:
    return state.has("Rocket Boots - Andy's Neighborhood", player)

def has_disc_launcher_construction(state: CollectionState, player: int) -> bool:
    return state.has("Disc Launcher - Construction Yard", player)

def has_grappling_hook_alleys(state: CollectionState, player: int) -> bool:
    return state.has("Grappling Hook - Alleys and Gullies", player)

def has_disc_launcher_alleys(state: CollectionState, player: int) -> bool:
    # The Disc Launcher in Alleys and Gullies spawns deep in the level: reaching
    # it needs Double Jump + Rope Sliding + Ledge Grab in addition to the item.
    return (state.has("Disc Launcher - Alleys and Gullies", player)
            and has_double_jump(state, player)
            and has_rope_sliding(state, player)
            and has_ledge_grab(state, player))

def has_rocket_boots_alleys(state: CollectionState, player: int) -> bool:
    return state.has("Rocket Boots - Alleys and Gullies", player)

def has_rocket_boots_toybarn(state: CollectionState, player: int) -> bool:
    return state.has("Rocket Boots - Al's Toy Barn", player)

def has_disc_launcher_toybarn(state: CollectionState, player: int) -> bool:
    # The Disc Launcher in Al's Toy Barn spawns past an obstacle: reaching its
    # pickup needs (Double Jump + Ledge Grab) OR Pole Climb, plus the item.
    return (state.has("Disc Launcher - Al's Toy Barn", player)
            and ((has_double_jump(state, player) and has_ledge_grab(state, player))
                 or has_pole_climb(state, player)))

def has_hover_boots_toybarn(state: CollectionState, player: int) -> bool:
    return state.has("Hover Boots - Al's Toy Barn", player)

def has_cosmic_shield_spaceland(state: CollectionState, player: int) -> bool:
    return state.has("Cosmic Shield - Al's Space Land", player)

def has_grappling_hook_elevator(state: CollectionState, player: int) -> bool:
    return state.has("Grappling Hook - Elevator Hop", player)

def has_cosmic_shield_penthouse(state: CollectionState, player: int) -> bool:
    return state.has("Cosmic Shield - Al's Penthouse", player)

def has_hover_boots_airport(state: CollectionState, player: int) -> bool:
    # The Hover Boots in Airport Infiltration spawn past a movement gate. Reaching
    # them needs Stomp + Double Jump + Pole Vault normally, or just Stomp + Double
    # Jump when Hard Skips are enabled — plus the item itself.
    if not state.has("Hover Boots - Airport Infiltration", player):
        return False
    if not (has_stomp(state, player) and has_double_jump(state, player)):
        return False
    try:
        skips = state.multiworld.worlds[player].options.skips.value
    except Exception:
        skips = SKIPS_OFF
    if skips == SKIPS_HARD:
        return True
    return has_pole_vault(state, player)

def has_rocket_boots_tarmac(state: CollectionState, player: int) -> bool:
    return state.has("Rocket Boots - Tarmac Trouble", player)

# ── GADGET LOOKUP BY NAME ─────────────────────────────────────

GADGET_CHECKERS = {
    "Cosmic Shield":        has_cosmic_shield_andys,
    "Rocket Boots":         has_rocket_boots_neighborhood,
    "Disc Launcher":        has_disc_launcher_construction,
    "Grappling Hook":       has_grappling_hook_alleys,
    "Hover Boots":          has_hover_boots_toybarn,
    "Cosmic Shield - Andy's House":        has_cosmic_shield_andys,
    "Rocket Boots - Andy's Neighborhood":  has_rocket_boots_neighborhood,
    "Disc Launcher - Construction Yard":   has_disc_launcher_construction,
    "Grappling Hook - Alleys and Gullies": has_grappling_hook_alleys,
    "Disc Launcher - Alleys and Gullies":  has_disc_launcher_alleys,
    "Rocket Boots - Alleys and Gullies":   has_rocket_boots_alleys,
    "Rocket Boots - Al's Toy Barn":        has_rocket_boots_toybarn,
    "Disc Launcher - Al's Toy Barn":       has_disc_launcher_toybarn,
    "Hover Boots - Al's Toy Barn":         has_hover_boots_toybarn,
    "Cosmic Shield - Al's Space Land":     has_cosmic_shield_spaceland,
    "Grappling Hook - Elevator Hop":       has_grappling_hook_elevator,
    "Cosmic Shield - Al's Penthouse":      has_cosmic_shield_penthouse,
    "Hover Boots - Airport Infiltration":  has_hover_boots_airport,
    "Rocket Boots - Tarmac Trouble":       has_rocket_boots_tarmac,
}

MOVE_CHECKERS = {
    "Laser":        has_laser,
    "Spin":         has_spin,
    "Stomp":        has_stomp,
    "Double Jump":  has_double_jump,
    "Pole Climb":   has_pole_climb,
    "Ledge Grab":   has_ledge_grab,
    "Pole Vault":   has_pole_vault,
    "Push":         has_push,
    "Rope Sliding": has_rope_sliding,
    "Visor":        has_visor,
}

# ── GENERIC MOVE/GADGET CHECKERS ─────────────────────────────

def has_move(state: CollectionState, player: int, move: str) -> bool:
    checker = MOVE_CHECKERS.get(move)
    if checker:
        return checker(state, player)
    return False

def has_all_moves(state: CollectionState, player: int, moves: List[str]) -> bool:
    return all(has_move(state, player, m) for m in moves)

def has_any_move(state: CollectionState, player: int, moves: List[str]) -> bool:
    return any(has_move(state, player, m) for m in moves)

def has_gadget_by_name(state: CollectionState, player: int, gadget: str, level: str = "") -> bool:
    # Try level-specific first
    if level:
        level_specific = {
            "Rocket Boots": {
                "Andy's Neighborhood":  has_rocket_boots_neighborhood,
                "Alleys and Gullies":   has_rocket_boots_alleys,
                "Al's Toy Barn":        has_rocket_boots_toybarn,
                "Tarmac Trouble":       has_rocket_boots_tarmac,
            },
            "Disc Launcher": {
                "Construction Yard":    has_disc_launcher_construction,
                "Alleys and Gullies":   has_disc_launcher_alleys,
                "Al's Toy Barn":        has_disc_launcher_toybarn,
            },
            "Grappling Hook": {
                "Alleys and Gullies":   has_grappling_hook_alleys,
                "Elevator Hop":         has_grappling_hook_elevator,
            },
            "Hover Boots": {
                "Al's Toy Barn":        has_hover_boots_toybarn,
                "Airport Infiltration": has_hover_boots_airport,
            },
            "Cosmic Shield": {
                "Andy's House":         has_cosmic_shield_andys,
                "Al's Space Land":      has_cosmic_shield_spaceland,
                "Al's Penthouse":       has_cosmic_shield_penthouse,
            },
        }
        if gadget in level_specific and level in level_specific[gadget]:
            return level_specific[gadget][level](state, player)
    checker = GADGET_CHECKERS.get(gadget)
    if checker:
        return checker(state, player)
    return False

def has_all_gadgets(state: CollectionState, player: int, gadgets: List[str], level: str = "") -> bool:
    return all(has_gadget_by_name(state, player, g, level) for g in gadgets)

def has_any_gadget(state: CollectionState, player: int, gadgets: List[str], level: str = "") -> bool:
    return any(has_gadget_by_name(state, player, g, level) for g in gadgets)

# ── TOKEN / TICKET HELPERS ────────────────────────────────────

def token_count(state: CollectionState, player: int) -> int:
    return state.count("Pizza Planet Token", player)

def ticket_count(state: CollectionState, player: int) -> int:
    return state.count("Final Showdown Ticket", player)

def boss_defeats(state: CollectionState, player: int) -> int:
    count = 0
    for boss_loc in ["Bombs Away! - Defeat Reward 1", "Slime Time - Defeat Reward 1",
                     "Toy Barn Encounter - Defeat Reward 1",
                     "The Evil Emperor Zurg - Defeat Reward 1"]:
        if state.can_reach(boss_loc, "Location", player):
            count += 1
    return count

# ============================================================
# LEVEL ACCESS RULES
# ============================================================

def final_showdown_goal_met(state: CollectionState, player: int, world: "ToyStory2World") -> bool:
    """Open mode: whether the Prospector Showdown is reachable, based on the
    chosen GOAL CONDITIONS. This mirrors the in-game unlock (computed in
    ts2_client.py and honored by check_prospector_unlock in the Lua): the final
    level opens on tokens / bosses / the Final Showdown Unlock item per the goal,
    NOT by always requiring the unlock item. (Previously can_access_level forced
    the Final Showdown Unlock onto every open-mode path, so a tokens-only goal
    still demanded the item — disagreeing with the actual game.)"""
    options = world.options
    goal = options.goal_conditions.value
    needs_tokens = goal in (GOAL_TOKENS, GOAL_T_AND_B, GOAL_T_AND_U, GOAL_T_B_U)
    needs_bosses = goal in (GOAL_BOSSES, GOAL_T_AND_B, GOAL_B_AND_U, GOAL_T_B_U)
    needs_unlock = goal in (GOAL_UNLOCK, GOAL_T_AND_U, GOAL_B_AND_U, GOAL_T_B_U)
    if needs_tokens and token_count(state, player) < options.final_showdown_token_gate.value:
        return False
    if needs_bosses and boss_defeats(state, player) < options.defeated_bosses_required.value:
        return False
    if needs_unlock and not state.has("Final Showdown Unlock", player):
        return False
    return True


def can_access_level(state: CollectionState, player: int, level: str, world: "ToyStory2World") -> bool:
    options = world.options
    mode = options.game_mode.value

    if mode == GAME_MODE_OPEN:
        # Only the randomly-chosen starting levels are free; everything else
        # needs its unlock item.
        starting = getattr(world, "_starting_levels", [])
        if level in starting:
            return True
        # The Prospector Showdown (final level) is special: it opens on the
        # chosen GOAL CONDITIONS (matching the in-game unlock), not on a
        # dedicated unlock item — except when the goal IS "level unlock", which
        # final_showdown_goal_met handles.
        if level == "Prospector Showdown":
            return final_showdown_goal_met(state, player, world)
        return state.has(f"{level} Unlock", player)

    else:  # Linear
        # Mirror the Lua's apply_linear_area exactly:
        #  - Area is reached by defeating the previous area's boss (the ticket).
        #  - REGULAR levels in an area are free once you're in that area (no token
        #    requirement) — the Lua unlocks AREA_UNLOCKED[area] with no token check.
        #  - The BOSS of an area needs tokens >= ITS OWN gate (gate 1..5), AND the
        #    previous boss defeated.
        #
        # IMPORTANT: a boss is "defeated" when you can ACCESS its level AND have the
        # attack to beat it. We compute this by recursing on can_access_level for
        # the (earlier) boss level — NOT via state.can_reach on the reward location.
        # Using can_reach here caused infinite recursion: reaching the reward
        # location re-evaluates its region's access rule, which re-enters
        # can_access_level, and the location-access wrapper closed the loop. Because
        # each area's gate only references strictly EARLIER areas, direct recursion
        # on can_access_level terminates.
        toks = token_count(state, player)

        def boss_defeated(boss_level: str, attack) -> bool:
            return can_access_level(state, player, boss_level, world) and attack()

        bombs_def = lambda: boss_defeated(
            "Bombs Away!", lambda: has_any_attack(state, player))
        slime_def = lambda: boss_defeated(
            "Slime Time", lambda: has_laser(state, player))
        tbe_def = lambda: boss_defeated(
            "Toy Barn Encounter",
            lambda: has_laser(state, player) and
                    has_any_move(state, player, ["Spin", "Stomp"]))
        zurg_def = lambda: boss_defeated(
            "The Evil Emperor Zurg", lambda: has_spin(state, player))

        # Area 0 starting levels
        if level in ("Andy's House", "Andy's Neighborhood"):
            return True
        # Area 0 boss
        elif level == "Bombs Away!":
            return toks >= options.bombs_away_token_gate.value
        # Area 1 regular (reached by beating Bombs Away)
        elif level in ("Construction Yard", "Alleys and Gullies"):
            return bombs_def()
        # Area 1 boss
        elif level == "Slime Time":
            return bombs_def() and toks >= options.slime_time_token_gate.value
        # Area 2 regular (reached by beating Slime Time)
        elif level in ("Al's Toy Barn", "Al's Space Land"):
            return slime_def()
        # Area 2 boss
        elif level == "Toy Barn Encounter":
            return slime_def() and toks >= options.toy_barn_encounter_token_gate.value
        # Area 3 regular (reached by beating Toy Barn Encounter)
        elif level in ("Elevator Hop", "Al's Penthouse"):
            return tbe_def()
        # Area 3 boss
        elif level == "The Evil Emperor Zurg":
            return tbe_def() and toks >= options.evil_emperor_zurg_token_gate.value
        # Area 4 regular (reached by beating Zurg)
        elif level in ("Airport Infiltration", "Tarmac Trouble"):
            return zurg_def()
        # Area 4 boss / final
        elif level == "Prospector Showdown":
            return zurg_def() and toks >= options.linear_final_showdown_token_gate.value
    return False

# ============================================================
# RULE COMPILER
# ============================================================
# Evaluates the canonical logic expressions from logic_data.py against a
# CollectionState. Movement leaves route through has_move; gadget leaves through
# has_gadget_by_name(level) so per-level gadget reachability still applies. Skip
# tiers are NESTED (Off < Easy < Hard < Insane): a location is reachable via its
# base logic, OR (skips>=Easy) its easy path, OR (skips>=Hard) its hard path, OR
# (skips>=Insane) its insane path. The misc gate (50 Coins / 5 Missing Toys /
# Missing <part>) is ANDed on top. Operators: '+' = AND (tightest), bare OR (and
# comma) = OR, bare AND = AND (loosest); parens override; "(always)" = no req.
import re as _re

_MOVES   = set(MOVE_CHECKERS.keys())
_GADGETS = set(GADGET_CHECKERS.keys())
_NAMES   = sorted(_MOVES | _GADGETS | {"Climb"}, key=len, reverse=True)
_ALIAS   = {"climb": "Pole Climb"}

class _PErr(Exception):
    pass

def _tok(s):
    toks = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1; continue
        if ch in "()+,":
            toks.append(ch); i += 1; continue
        m = _re.match(r'(?i)(or|and)\b', s[i:])
        if m:
            toks.append(m.group(1).upper()); i += len(m.group(1)); continue
        hit = None
        for nm in _NAMES:
            if s[i:i + len(nm)].lower() == nm.lower():
                hit = nm; break
        if hit:
            toks.append(("I", _ALIAS.get(hit.lower(), hit))); i += len(hit); continue
        raise _PErr("bad token: %r" % s[i:i + 20])
    return toks

class _P:
    def __init__(self, t):
        self.t = t; self.i = 0
    def pk(self):
        return self.t[self.i] if self.i < len(self.t) else None
    def eat(self, x=None):
        t = self.pk()
        if x is not None and t != x:
            raise _PErr("expected %r" % x)
        self.i += 1; return t
    def go(self):
        a = self.a()
        if self.i != len(self.t):
            raise _PErr("trailing tokens")
        return a
    def a(self):            # bare AND (loosest): joins OR-layers
        p = [self.o()]
        while self.pk() == "AND":
            self.eat(); p.append(self.o())
        return p[0] if len(p) == 1 else ("and", p)
    def o(self):            # OR-layer (comma is also OR)
        p = [self.pl()]
        while self.pk() in ("OR", ","):
            while self.pk() in ("OR", ","):
                self.eat()
            p.append(self.pl())
        return p[0] if len(p) == 1 else ("or", p)
    def pl(self):           # '+' AND (tightest)
        p = [self.at()]
        while self.pk() == "+":
            self.eat(); p.append(self.at())
        return p[0] if len(p) == 1 else ("and", p)
    def at(self):
        t = self.pk()
        if t == "(":
            self.eat("("); a = self.a(); self.eat(")"); return a
        if isinstance(t, tuple):
            self.eat(); return ("leaf", t[1])
        raise _PErr("expected atom, got %r" % (t,))

def _parse(s):
    s = (s or "").strip()
    if not s or s == "(always)":
        return ("true",)
    return _P(_tok(s)).go()

def _ev(ast, state, player, level):
    k = ast[0]
    if k == "true":
        return True
    if k == "leaf":
        n = ast[1]
        if n in _GADGETS:
            return has_gadget_by_name(state, player, n, level)
        return has_move(state, player, n)
    if k == "and":
        return all(_ev(c, state, player, level) for c in ast[1])
    return any(_ev(c, state, player, level) for c in ast[1])

# Pre-parse every location's expressions once at import.
_COMPILED = {}   # name -> (level, logic, easy|None, hard|None, insane|None, misc)
for _loc in ALL_LOCATIONS:
    _COMPILED[_loc.name] = (
        _loc.level,
        _parse(_loc.logic),
        _parse(_loc.easy) if _loc.easy else None,
        _parse(_loc.hard) if _loc.hard else None,
        _parse(_loc.insane) if _loc.insane else None,
        _loc.misc,
    )

# Conditional misc gate: "50 Coins and <move-expr> if Coinsanity is off" -- the
# extra requirement applies only when Coinsanity is disabled (with it on, coins
# carry over across resets so the extra movement isn't strictly needed).
import re as _re
_COINOFF_RE = _re.compile(r"^50 Coins and (.+) if Coinsanity is off$")

def _reach(state, player, name, skips):
    """Movement/gadget reachability for a location (logic + skip tiers), no misc."""
    c = _COMPILED.get(name)
    if c is None:
        return True
    lvl, la, ea, ha, ia, _ = c
    if _ev(la, state, player, lvl):
        return True
    if ea is not None and skips >= SKIPS_EASY and _ev(ea, state, player, lvl):
        return True
    if ha is not None and skips >= SKIPS_HARD and _ev(ha, state, player, lvl):
        return True
    if ia is not None and skips >= SKIPS_INSANE and _ev(ia, state, player, lvl):
        return True
    return False

def _fifty_coins_ok(state, player, level, skips, world):
    """The "50 coins in this level" half of a Hamm token (Coinsanity-aware)."""
    options = world.options
    coins = COIN_DATA.get(level, [])
    if len(coins) < 50:
        return False
    if options.coinsanity.value:
        recv = options.coinsanity_received_bundle_size.value or 5
        import math as _math
        return state.has(f"Coin Bundle - {level}", player, _math.ceil(50 / recv))
    return sum(1 for c in coins if _reach(state, player, c.name, skips)) >= 50

def location_access_rule(name, world):
    """Full access rule for a sheet location: reachability + misc gate."""
    player = world.player
    level  = _COMPILED[name][0]
    misc   = _COMPILED[name][5]
    _m = _COINOFF_RE.match(misc or "")
    coinoff_extra = _parse(_m.group(1)) if _m else None   # extra move when Coinsanity off
    def fn(state):
        skips = world.options.skips.value
        if not _reach(state, player, name, skips):
            return False
        if misc == "50 Coins" or coinoff_extra is not None:
            if not _fifty_coins_ok(state, player, level, skips, world):
                return False
            if coinoff_extra is not None and not world.options.coinsanity.value:
                if not _ev(coinoff_extra, state, player, level):
                    return False
            return True
        if misc == "5 Missing Toys":
            return has_all_level_toys(state, player, level)
        if misc.startswith("Missing "):
            return state.has(misc, player)
        return True
    return fn

def can_reach_coin(state, player, level, coin_idx, skips):
    """True if the coin at 0-based coin_idx in `level` is logically reachable."""
    coins = COIN_DATA.get(level, [])
    if coin_idx >= len(coins):
        return True
    return _reach(state, player, coins[coin_idx].name, skips)


def coin_bundle_rule(
    state: CollectionState,
    player: int,
    level: str,
    bundle_num: int,   # 1-based
    bundle_size: int,  # 0 = ALL
    skips: int,
    world: "ToyStory2World",
) -> bool:
    coins = COIN_DATA.get(level, [])
    total = len(coins)
    if total == 0:
        return True

    if bundle_size == 0:
        # ALL — must reach every coin
        return all(can_reach_coin(state, player, level, i, skips) for i in range(total))

    # Player needs ANY (bundle_num * bundle_size) coins total in this level.
    # The last bundle may be a partial (e.g. 103 coins, size 5 -> bundle 21 needs all 103).
    coins_needed = min(bundle_num * bundle_size, total)

    # Count how many coins are reachable with current state.
    # Short-circuit as soon as we have enough.
    reachable = 0
    for i in range(total):
        if can_reach_coin(state, player, level, i, skips):
            reachable += 1
            if reachable >= coins_needed:
                return True
    return False


# ============================================================
# HAMM'S 50 COINS TOKEN RULE
# ============================================================

def hamms_50_coins_rule(
    state: CollectionState,
    player: int,
    level: str,
    skips: int,
    moves_and: List[str],
    moves_or: List[str],
    world: "ToyStory2World",
    move_check=None,
) -> bool:
    """Player needs 50 coins in this level AND the move requirements to reach
    Hamm. How "50 coins" is satisfied depends on Coinsanity:
      - Coinsanity ON: coins come from received Coin Bundle items. Need enough
        bundles for this level that bundles * received_bundle_size >= 50.
      - Coinsanity OFF: coins are collected in-level, so need 50 of the level's
        coins to be physically reachable.
    move_check, if given, is a callable(state, player) -> bool used INSTEAD of
    moves_and/moves_or, for movement logic the flat lists can't express (e.g.
    "Pole Climb OR (Double Jump AND Ledge Grab)")."""
    options = world.options
    coins = COIN_DATA.get(level, [])
    level_max = len(coins)
    # A level with fewer than 50 coins can never satisfy Hamm either way.
    if level_max < 50:
        return False

    if options.coinsanity.value:
        # Need enough received bundles for THIS level to total >= 50 coins.
        recv_size = options.coinsanity_received_bundle_size.value or 5
        import math as _math
        bundles_needed = _math.ceil(50 / recv_size)
        if not state.has(f"Coin Bundle - {level}", player, bundles_needed):
            return False
    else:
        reachable = sum(1 for i in range(level_max)
                        if can_reach_coin(state, player, level, i, skips))
        if reachable < 50:
            return False

    if move_check is not None:
        return move_check(state, player)
    ma_ok = has_all_moves(state, player, moves_and) if moves_and else True
    mo_ok = has_any_move(state, player, moves_or) if moves_or else True
    return ma_ok and mo_ok

# ============================================================
# MISSING TOYS TOKEN RULE
# ============================================================

# Level -> the toy item name whose 5 copies gate that level's Missing Toys Token
MISSING_TOYS_TOKEN_ITEM = {
    "Andy's House":          "Sheep",
    "Andy's Neighborhood":   "Soldier",
    "Construction Yard":     "Worker Tike",
    "Alleys and Gullies":    "Duck",
    "Al's Toy Barn":         "Chick",
    "Al's Space Land":       "Alien",
    "Elevator Hop":          "Mouse",
    "Al's Penthouse":        "Critter",
    "Airport Infiltration":  "Passenger Tike",
    "Tarmac Trouble":        "Luggage",
}

def has_all_level_toys(state: CollectionState, player: int, level: str) -> bool:
    """True if the player owns all 5 of a level's missing toys, honoring the
    missing_toy_bundle_size option: individual mode needs 5 copies of the base
    toy; bundle mode needs 1 copy of that level's "5 <toy>" item."""
    base = MISSING_TOYS_TOKEN_ITEM.get(level)
    if not base:
        return True
    world = state.multiworld.worlds[player]
    if getattr(world.options, "missing_toy_bundle_size", None) and \
            world.options.missing_toy_bundle_size.value == 5:
        return state.has(TOY_BUNDLE_NAME[base], player, 1)
    return state.has(base, player, 5)

def missing_toys_token_rule(
    state: CollectionState,
    player: int,
    level: str,
    moves_and: List[str],
    moves_or: List[str],
    gadgets_and: List[str],
) -> bool:
    """Player needs all 5 of the level's missing toys (the toy ITEMS) AND the
    movement/gadgets to reach the token. The 5-toy requirement is real AP logic
    now that toys are received items — previously this only checked movement, so
    the tracker thought the token was reachable with zero toys."""
    if not has_all_level_toys(state, player, level):
        return False
    ma_ok = has_all_moves(state, player, moves_and) if moves_and else True
    mo_ok = has_any_move(state, player, moves_or) if moves_or else True
    ga_ok = has_all_gadgets(state, player, gadgets_and, level) if gadgets_and else True
    return ma_ok and mo_ok and ga_ok

# ============================================================
# GIVE POTATO HEAD RULE
# ============================================================

def give_potato_head_rule(
    state: CollectionState,
    player: int,
    part_item: str,
    moves_and: List[str] = None,
) -> bool:
    """Player must have the missing part AP item, plus any move requirements."""
    if not state.has(part_item, player):
        return False
    if moves_and:
        return has_all_moves(state, player, moves_and)
    return True

# ============================================================
# GOAL RULE
# ============================================================

def goal_rule(state: CollectionState, player: int, world: "ToyStory2World") -> bool:
    options = world.options
    mode = options.game_mode.value
    skips = options.skips.value

    # Goal always requires Stomp OR Spin at Prospector
    if not (has_stomp(state, player) or has_spin(state, player)):
        return False

    # Must be able to access Prospector Showdown
    if not can_access_level(state, player, "Prospector Showdown", world):
        return False

    if mode == GAME_MODE_LINEAR:
        return True

    # Open mode — check goal conditions
    goal = options.goal_conditions.value
    token_gate = options.final_showdown_token_gate.value
    boss_req   = options.defeated_bosses_required.value

    needs_tokens = goal in (GOAL_TOKENS, GOAL_T_AND_B, GOAL_T_AND_U, GOAL_T_B_U)
    needs_bosses = goal in (GOAL_BOSSES, GOAL_T_AND_B, GOAL_B_AND_U, GOAL_T_B_U)
    needs_unlock = goal in (GOAL_UNLOCK, GOAL_T_AND_U, GOAL_B_AND_U, GOAL_T_B_U)

    if needs_tokens and token_count(state, player) < token_gate:
        return False
    if needs_bosses and boss_defeats(state, player) < boss_req:
        return False
    if needs_unlock and not state.has("Final Showdown Unlock", player):
        return False

    return True

# ============================================================
# SET RULES
# ============================================================

def set_rules(world: "ToyStory2World") -> None:
    multiworld  = world.multiworld
    player      = world.player
    options     = world.options
    skips       = options.skips.value
    bundle_size = options.coinsanity_checks_bundle_size.value

    def rule(loc_name, fn):
        # A location may not exist if the sanity that creates it is disabled
        # (e.g. lifesanity off -> no "Life (...)" locations), or if a coin's
        # descriptive location only exists in 1-coin mode. get_location raises
        # KeyError in that case, so swallow it and skip applying the rule.
        #
        # NAME-FORMAT BRIDGE: logic_data (the rule source) writes sublocations with
        # a "(X)" suffix — e.g. "Airport Infiltration - Battery (Luggage Pile)" —
        # but the REGISTERED location in LOCATION_TABLE uses " - X":
        # "Airport Infiltration - Battery - Luggage Pile". A direct get_location on
        # the paren form raised KeyError, the rule was silently skipped, and the
        # location got NO access rule -> always reachable (a sphere-1 free check for
        # every battery/life/toy/hint-block/luggage with a parenthesised spot). On
        # miss we retry with the paren->dash form so the rule attaches regardless of
        # which format logic_data uses. The rule fn itself still closes over the
        # original logic_data name (its _COMPILED entry is keyed that way).
        try:
            loc = multiworld.get_location(loc_name, player)
        except KeyError:
            alt = re.sub(r' \(([^)]+)\)', r' - \1', loc_name)
            if alt == loc_name:
                return
            try:
                loc = multiworld.get_location(alt, player)
            except KeyError:
                return
        if loc:
            loc.access_rule = fn

    # ── COIN BUNDLE LOCATIONS (only when bundle_size != 1) ───────
    # In 1-coin mode every coin is its own descriptive location, handled by the
    # compiled-rule loop below. In bundle mode the seed instead has milestone
    # "<Level> - Coin Bundle N" locations gated on reaching N of the level's coins.
    if bundle_size != 1:
        for loc in multiworld.get_locations(player):
            if " - Coin Bundle " not in loc.name:
                continue
            for lvl in COIN_DATA:
                if loc.name.startswith(lvl + " - Coin Bundle "):
                    bn = int(loc.name.rsplit("Coin Bundle ", 1)[1])
                    loc.access_rule = (lambda state, l=lvl, b=bn:
                        coin_bundle_rule(state, player, l, b, bundle_size, skips, world))
                    break

    # ── ALL SHEET LOCATIONS (data-driven compiled rules) ────────
    # Every coin and non-coin location's reachability — base logic, the Easy/
    # Hard/Insane skip tiers, and the 50-coins / 5-toys / potato-part misc gates —
    # comes straight from logic_data.py via the compiler. Locations absent from
    # this seed (option disabled, or coins while in bundle mode) are skipped by
    # rule()'s KeyError guard, so this single loop is safe for every option combo.
    for _loc in ALL_LOCATIONS:
        rule(_loc.name, location_access_rule(_loc.name, world))

    # ── GOAL / VICTORY ──────────────────────────────────────────
    # "Prospector Showdown - Defeat GOAL" is created in code (not in the sheet)
    # and carries the locked Victory item; its rule is the full goal condition.
    rule("Prospector Showdown - Defeat GOAL",
         lambda state: goal_rule(state, player, world))

    # NOTE: Level access is enforced by each region's ENTRANCE rule (the World's
    # set_rules method sets "To <level>" entrances to can_access_level), so a
    # location is only reachable once its region is. We deliberately do NOT also
    # wrap every location's access_rule with can_access_level here — doing so
    # previously caused infinite recursion through the boss-defeat checks.