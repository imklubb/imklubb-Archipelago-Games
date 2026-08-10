#!/usr/bin/env python3
"""Rebuild the PopTracker pack's logic from the world's own logic.py.

    py -3.13 taz_tracker_gen.py --pack "..\\Taz Wanted\\Taz-Wanted-Poptracker\\Taz-Wanted-Poptracker"

WHY THIS EXISTS
---------------
locations.json and ap_sections.lua both say AUTO-GENERATED at the top, but the
thing that generated them was not checked in anywhere, so the tracker's logic
had drifted from the world's and there was no way to re-derive it. This is that
generator, written down this time.

It is a TRANSFORM, not a rebuild. Every pin coordinate in locations.json was
placed by hand on a map image and none of it can be derived from anything --
so the groups, their map_locations and their images are read from the existing
file and preserved exactly. What gets rewritten is only the part that is a
consequence of the world's rules: the access rules, and which sections exist.

WHAT IT FIXES
-------------
1. BONUS GAMES were gated on the level's unlock. The world puts a bonus game
   in the HUB in Open mode -- the booth is outside the level entrance -- so it
   needs the Bonus Game Unlock and nothing else. Gating it on the level meant a
   check the player had already been given read as out of logic.

2. TAZLAND'S FIRST CHECKS were gated on the Tazland unlock. The world splits
   Tazland in two, because the bridge is what the geofence blocks: nine
   sandwiches on Standard and 2% destruction sit before it. Those belong to
   the entrance and need nothing.

3. LINEAR SEEDS HAD NO LOGIC AT ALL. Every rule in the pack was an
   unlock_<level> item, and Linear does not put level unlocks in the pool --
   progression is poster gates and beaten bosses. So every location in a Linear
   seed was permanently out of logic. Same trap as catcher_refused, which is
   already exempt from the unlock test for exactly this reason.

4. SANDWICH CHECKS = 1 makes every sandwich a check: 100 a level. Those are not
   drawn as a hundred pins. The per-threshold sections are hidden and a single
   counting section per level is shown instead.

The rules become Lua calls into ap_logic.lua, which this also writes, so that
one mode-aware place mirrors logic.py rather than a rule being baked into three
hundred JSON nodes.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")


def load_world():
    """The world's logic.py, which is the only source of truth for the rules."""
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("_imports", "logic"):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.logic"]


def slug(s):
    """The same code autotracking.lua's sl() produces, so the names match.

        s:lower():gsub("[^a-z0-9]+", "_"):gsub("^_+", ""):gsub("_+$", "")
    """
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ---------------------------------------------------------------- the rules


def linear_table(D):
    """slug -> (posters needed, the boss that must be beaten first).

    Read straight out of LINEAR_ORDER and the gate options, in the same walk
    entrance_rules does, so the two cannot drift:

        prev_gate starts at 0 and becomes this boss's gate afterwards
        prev_boss starts at None and becomes this boss afterwards
    """
    gate_opt = {7: "gate_elephant_pong", 12: "gate_gladiatoons",
                17: "gate_dodge_city", 19: "gate_disco_volcano",
                20: "gate_disco_volcano"}
    levels, bosses = {}, {}
    prev_gate, prev_boss = "0", None
    for _hub, lids, boss in D.LINEAR_ORDER:
        for lid in lids:
            name = D.LEVEL_NAME.get(lid)
            if name:
                levels[slug(name)] = (prev_gate, prev_boss)
        short = D.BOSS_UNLOCK[boss].replace(" Unlock", "")
        bosses[slug(short)] = (gate_opt[boss], prev_boss)
        prev_gate = gate_opt[boss]
        prev_boss = slug(short)
    # The Hindenbird needs the last gate and Disco Volcano beaten.
    hb = D.BOSS_UNLOCK[20].replace(" Unlock", "")
    bosses[slug(hb)] = (gate_opt[20], slug(
        D.BOSS_UNLOCK[19].replace(" Unlock", "")))
    return levels, bosses


def boss_section_paths(D, doc):
    """slug -> the tracker path of that boss's own defeat check.

    "Beaten" is an AP event living in the boss's region, so what it really
    means is "that region is reachable". The Lua works that out recursively;
    these paths are only used for the display name, but they are generated
    rather than typed so a renamed boss cannot silently break the table.
    """
    out = {}
    for lid, locname in D.BOSSES:
        short = D.BOSS_UNLOCK[lid].replace(" Unlock", "")
        out[slug(short)] = locname
    return out


# ------------------------------------------------------------- the transform


def transform(doc, D):
    """Rewrite the access rules and the sandwich sections, in place.

    Returns a list of what changed, so a run that does nothing says so rather
    than silently rewriting a file with identical contents.
    """
    changes = []
    tazland = slug(D.LEVEL_NAME[D.TAZLAND])
    bonus_slugs = {slug(n) for lid, n in D.LEVELS if lid not in D.NO_BONUS}
    level_slugs = {slug(n) for _lid, n in D.LEVELS}
    boss_slugs = {slug(D.BOSS_UNLOCK[lid].replace(" Unlock", ""))
                  for lid, _ in D.BOSSES}

    def rules_of(node):
        return [tuple(a) for a in (node.get("access_rules") or [])]

    def set_rules(node, rules):
        if rules:
            node["access_rules"] = [list(r) for r in rules]
        else:
            node.pop("access_rules", None)

    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n.get("children", []))

    for node in walk(doc):
        name = node.get("name", "")
        secs = node.get("sections", [])
        cur = rules_of(node)

        # ---- a group gated on a level unlock
        unlock = None
        for r in cur:
            if len(r) == 1 and r[0].startswith("unlock_"):
                unlock = r[0][len("unlock_"):]
        if unlock and unlock in level_slugs:
            is_bonus = any("Bonus Game" in s.get("name", "") for s in secs)
            if is_bonus:
                # The booth is outside the level entrance. In Open the check
                # belongs to the hub and needs only the unlock item, which the
                # section already carries; in Linear it is still in the level.
                # $reachBonus expresses both.
                set_rules(node, [r for r in cur if r != (f"unlock_{unlock}",)])
                for s in secs:
                    own = rules_of(s)
                    bs = [r[0][len("bonus_"):] for r in own
                          if len(r) == 1 and r[0].startswith("bonus_")]
                    if bs:
                        s["access_rules"] = [[f"$reachBonus|{bs[0]}"]]
                changes.append(f"bonus group {name!r}: level unlock removed")
            elif unlock == tazland and any(
                    _is_threshold(s.get("name", ""))[0] for s in secs):
                # [0], not the tuple. _is_threshold returns (None, None) for
                # anything that is not a threshold, and a two-element tuple is
                # truthy -- so testing it whole made this branch fire for every
                # Tazland group including the catchers, whose costume rule the
                # else below then overwrote.
                # Tazland is two regions. Which side a threshold is on depends
                # on the difficulty and the starting sandwiches, so it has to
                # be decided per section at runtime, not baked in here.
                set_rules(node, [r for r in cur if r != (f"unlock_{unlock}",)])
                for s in secs:
                    kind, thr = _is_threshold(s.get("name", ""))
                    if kind:
                        s["access_rules"] = [[f"$reachTazland|{kind}|{thr}"]]
                    else:
                        # The main map's "- Extras" pin mixes thresholds with
                        # the statue and Level Complete. Dropping the group
                        # rule without giving those one of their own would
                        # leave them gated on nothing at all.
                        #
                        # Whatever the section already required is kept, and
                        # kept as an AND: access_rules is an OR of ANDs, so
                        # the new term joins each existing inner list rather
                        # than becoming an alternative to it. Appending it as
                        # its own list would mean "costume OR reachable",
                        # which is not a gate at all.
                        own = rules_of(s)
                        s["access_rules"] = (
                            [list(r) + [f"$reach|{unlock}"] for r in own]
                            if own else [[f"$reach|{unlock}"]])
                changes.append(f"Tazland group {name!r}: split per threshold")
            else:
                set_rules(node, [(f"$reach|{unlock}",) if r ==
                                 (f"unlock_{unlock}",) else r for r in cur])
                changes.append(f"group {name!r}: unlock_{unlock} -> $reach")

        # ---- boss checks
        for s in secs:
            own = rules_of(s)
            new = []
            for r in own:
                if len(r) == 1 and r[0].startswith("boss_") \
                        and r[0][len("boss_"):] in boss_slugs:
                    new.append((f"$reachBoss|{r[0][len('boss_'):]}",))
                    changes.append(f"boss {s['name']!r}: -> $reachBoss")
                elif len(r) == 1 and r[0] == "$hindenbirdOpen":
                    # Reaching the fight is the unlock; WINNING is the goal,
                    # and the goal is not an access rule. Same reasoning as
                    # entrance_rules, which deliberately does not use the goal
                    # here either.
                    new.append((f"$reachBoss|"
                                f"{slug(D.BOSS_UNLOCK[20].replace(' Unlock', ''))}",))
                    changes.append(f"boss {s['name']!r}: $hindenbirdOpen "
                                   "-> $reachBoss")
                else:
                    new.append(r)
            set_rules(s, new)
        if any(len(r) == 1 and r[0] == "$hindenbirdOpen" for r in rules_of(node)):
            set_rules(node, [(f"$reachBoss|"
                              f"{slug(D.BOSS_UNLOCK[20].replace(' Unlock', ''))}",)
                             if r == ("$hindenbirdOpen",) else r
                             for r in rules_of(node)])

    # ---- the Hindenbird Tickets
    #
    # A second check per boss, which exists only when bosses are part of the
    # goal. The pack never had a section for any of them, so in a bosses-goal
    # seed four checks silently did not track -- found by the coverage check
    # below rather than by anyone noticing.
    tickets = 0
    for node in walk(doc):
        secs = node.get("sections", [])
        for s in list(secs):
            nm = s.get("name", "")
            if not nm.startswith("BOSS ") or nm.endswith("Hindenbird Ticket"):
                continue
            lid = next((l for l, n in D.BOSSES if n == nm), None)
            if lid is None or lid == D.HINDENBIRD_LEVEL:
                continue                  # beating Tweety IS the goal
            tname = f"{nm} - Hindenbird Ticket"
            if any(x.get("name") == tname for x in secs):
                continue
            secs.append({
                "name": tname,
                "item_count": 1,
                "chest_unopened_img": "images/items/tickets.png",
                "chest_opened_img": s.get("chest_opened_img"),
                "visibility_rules": [["$ticketVisible"]],
                "access_rules": [list(r) for r in rules_of(s)],
            })
            tickets += 1
    changes.append(f"added {tickets} Hindenbird Ticket section(s)")

    # ---- the counters, one per level per kind, shown only at an interval of 1
    #
    # Every sandwich and every whole percent of destruction being its own check
    # is a hundred pins a level either way, which is unreadable. At an interval
    # of 1 the per-threshold pins stand down and one counting section takes
    # their place.
    added = 0
    for kind, word, vis in (("s", "Sandwiches", "$sandwichCounterVisible"),
                            ("d", "Destruction", "$destructionCounterVisible")):
        for node in walk(doc):
            secs = node.get("sections", [])
            levels = {_level_of(s.get("name", ""), D) for s in secs
                      if _is_threshold(s.get("name", ""))[0] == kind}
            levels.discard(None)
            for lvl in sorted(levels):
                cname = f"{lvl} - {word}"
                if any(s.get("name") == cname for s in secs):
                    continue
                model = next(s for s in secs
                             if _is_threshold(s.get("name", ""))[0] == kind
                             and _level_of(s.get("name", ""), D) == lvl)
                sec = {
                    "name": cname,
                    "item_count": D.SANDWICH_GOAL,
                    "chest_unopened_img": model.get("chest_unopened_img"),
                    "chest_opened_img": model.get("chest_opened_img"),
                    "visibility_rules": [[vis]],
                }
                if lvl == D.LEVEL_NAME[D.TAZLAND]:
                    sec["access_rules"] = [[f"$reachTazland|{kind}|1"]]
                secs.append(sec)
                added += 1
    changes.append(f"added {added} counter section(s)")
    return changes


_THRESH = re.compile(r"^(.*) - (\d+)(%?) (Sandwiches|Destruction)$")


def _is_threshold(name):
    """('s'|'d', threshold) for a per-threshold section, else (None, None)."""
    m = _THRESH.match(name or "")
    if not m:
        return None, None
    return ("d" if m.group(4) == "Destruction" else "s"), int(m.group(2))


def _level_of(name, D):
    m = _THRESH.match(name or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ emitters


def emit_sections(doc):
    """Archipelago location name -> the section paths that show it.

    A location appears on its level map and again on the main map's grouped
    pin, and at interval 1 every sandwich also feeds its level's counter --
    which is why this maps to a list rather than to a single path.
    """
    out, counter_paths = {}, {}
    for top in doc:
        # The pack is exactly two deep -- "@<map>/<group>/<section>" -- and
        # every path in the shipped ap_sections.lua has two slashes, so this
        # asserts rather than guessing at a general walk.
        assert not top.get("sections"), \
            f"{top.get('name')!r} has sections at the top level"
        for group in top.get("children", []):
            assert not group.get("children"), \
                f"{group.get('name')!r} has children; the pack is 2 deep"
            base = f"@{top['name']}/{group['name']}"
            names = [s.get("name") for s in group.get("sections", [])]
            # Keyed by (kind, level), then expanded to every location name the
            # world can emit for that kind. Keying it by the SECTION names
            # would only ever cover the twenty thresholds that have a pin of
            # their own -- and at an interval of 1 the other eighty are real
            # locations that must still count.
            for nm in names:
                for kind, word in (("s", " - Sandwiches"),
                                   ("d", " - Destruction")):
                    if nm and nm.endswith(word) \
                            and _is_threshold(nm)[0] is None:
                        counter_paths.setdefault(
                            (kind, nm[:-len(word)]), []).append(f"{base}/{nm}")
            for nm in names:
                if nm in _counter_names(names):
                    continue              # a counter is not a location
                out.setdefault(nm, []).append(f"{base}/{nm}")
    return out, counter_paths


def _counter_names(names):
    """The counter sections among a group's section names."""
    return {n for n in names
            if n and _is_threshold(n)[0] is None
            and (n.endswith(" - Sandwiches") or n.endswith(" - Destruction"))}


def lua_table(name, mapping, doc_lines):
    out = ["\n".join("-- " + l for l in doc_lines), f"{name} = {{"]
    for k in sorted(mapping):
        vals = ", ".join(f'"{v}"' for v in mapping[k])
        out.append(f'  ["{k}"] = {{{vals}}},')
    out.append("}")
    return "\n".join(out)


def emit_logic_lua(D):
    levels, bosses = linear_table(D)
    pre = D.TAZLAND_PREGATE
    L = []
    L.append('-- AUTO-GENERATED by taz_tracker_gen.py. Do not edit by hand.')
    L.append('--')
    L.append('-- The world\'s rules, mirrored. Everything here comes out of')
    L.append('-- logic.py: LINEAR_ORDER, the gate options and TAZLAND_PREGATE.')
    L.append('')
    L.append('local function on(v) return v == true or v == 1 end')
    L.append('local function sd() return SLOT_DATA or {} end')
    L.append('local function linear() return sd()["game_mode"] == "linear" end')
    L.append('local function has(code)')
    L.append('  local o = Tracker:FindObjectForCode(code)')
    L.append('  return (o and o.Active) and true or false')
    L.append('end')
    L.append('local function posters()')
    L.append('  local o = Tracker:FindObjectForCode("poster")')
    L.append('  return o and o.AcquiredCount or 0')
    L.append('end')
    L.append('')
    L.append('-- level slug -> {poster gate option, the boss that must be')
    L.append('-- beaten first}. Straight out of LINEAR_ORDER.')
    L.append('local LINEAR_LEVEL = {')
    for k in sorted(levels):
        gate, boss = levels[k]
        g = gate if gate == "0" else f'"{gate}"'
        b = "nil" if boss is None else f'"{boss}"'
        L.append(f'  ["{k}"] = {{{g}, {b}}},')
    L.append('}')
    L.append('local LINEAR_BOSS = {')
    for k in sorted(bosses):
        gate, boss = bosses[k]
        g = gate if gate == "0" else f'"{gate}"'
        b = "nil" if boss is None else f'"{boss}"'
        L.append(f'  ["{k}"] = {{{g}, {b}}},')
    L.append('}')
    L.append('')
    L.append('-- Sandwiches and destruction reachable in Tazland BEFORE the')
    L.append('-- bridge, by difficulty. Measured in game.')
    L.append('local TAZLAND_PRE = {')
    for diff in ("standard", "advanced", "expert"):
        v = pre[diff]
        L.append(f'  {diff} = {{s = {v["sandwiches"]}, d = {v["destruction"]}}},')
    L.append('}')
    L.append(f'local TAZLAND = "{slug(D.LEVEL_NAME[D.TAZLAND])}"')
    L.append('')
    L.append(f'local HINDENBIRD = "{slug(D.BOSS_UNLOCK[20].replace(" Unlock", ""))}"')
    L.append('')
    L.append('-- The last fight in Open is not something an item hands over.')
    L.append('-- It is what the player set out to qualify for, so whatever')
    L.append('-- they picked as their goal is what opens it -- and when they')
    L.append('-- did not pick the unlock, that item is not in the seed at all,')
    L.append('-- so testing for it would keep the fight shut for the run.')
    L.append('function hindenbirdGoal()')
    L.append('  local d = sd()')
    L.append('  if on(d["goal_posters_enabled"]) then')
    L.append('    if posters() < (tonumber(d["goal_posters"]) or 50) then')
    L.append('      return 0')
    L.append('    end')
    L.append('  end')
    L.append('  if on(d["goal_bosses_enabled"]) then')
    L.append('    local t = Tracker:FindObjectForCode("ticket")')
    L.append('    local need = tonumber(d["goal_bosses"]) or 4')
    L.append('    if not t or t.AcquiredCount < need then return 0 end')
    L.append('  end')
    L.append('  if on(d["goal_unlock_enabled"]) then')
    L.append('    if not has("boss_" .. HINDENBIRD) then return 0 end')
    L.append('  end')
    L.append('  -- normalise turns the unlock goal on when nothing is chosen,')
    L.append('  -- so a seed with no conditions at all cannot reach here.')
    L.append('  if not (on(d["goal_posters_enabled"])')
    L.append('          or on(d["goal_bosses_enabled"])')
    L.append('          or on(d["goal_unlock_enabled"])) then')
    L.append('    return has("boss_" .. HINDENBIRD) and 1 or 0')
    L.append('  end')
    L.append('  return 1')
    L.append('end')
    L.append('')
    L.append('local function gateValue(g)')
    L.append('  if type(g) == "number" then return g end')
    L.append('  return tonumber(sd()[g]) or 0')
    L.append('end')
    L.append('')
    L.append('-- Reaching a boss. In Open that is its unlock item. In Linear it')
    L.append('-- is a poster gate plus the previous boss, and "beaten" there')
    L.append('-- means the same thing it means to the world: that boss is')
    L.append('-- REACHABLE. The world uses an event placed in the boss\'s own')
    L.append('-- region, and having an event is exactly reaching it -- so this')
    L.append('-- recurses rather than reading whether the check is ticked.')
    L.append('function reachBoss(s)')
    L.append('  if not linear() then')
    L.append('    if s == HINDENBIRD then return hindenbirdGoal() end')
    L.append('    return has("boss_" .. s) and 1 or 0')
    L.append('  end')
    L.append('  local r = LINEAR_BOSS[s]')
    L.append('  if not r then return 1 end')
    L.append('  if posters() < gateValue(r[1]) then return 0 end')
    L.append('  if r[2] and reachBoss(r[2]) == 0 then return 0 end')
    L.append('  return 1')
    L.append('end')
    L.append('')
    L.append('-- Reaching a level. Open: its unlock item. Linear: no unlock')
    L.append('-- items exist at all, so testing for one put every location in')
    L.append('-- the seed permanently out of logic.')
    L.append('function reach(s)')
    L.append('  if not linear() then return has("unlock_" .. s) and 1 or 0 end')
    L.append('  local r = LINEAR_LEVEL[s]')
    L.append('  if not r then return 1 end')
    L.append('  if posters() < gateValue(r[1]) then return 0 end')
    L.append('  if r[2] and reachBoss(r[2]) == 0 then return 0 end')
    L.append('  return 1')
    L.append('end')
    L.append('')
    L.append('-- A bonus game is played at a booth OUTSIDE its level, so in')
    L.append('-- Open it belongs to the hub and needs only its unlock. In')
    L.append('-- Linear the world leaves it in the level, so the level counts.')
    L.append('function reachBonus(s)')
    L.append('  if not has("bonus_" .. s) then return 0 end')
    L.append('  if linear() then return reach(s) end')
    L.append('  return 1')
    L.append('end')
    L.append('')
    L.append('-- Tazland is two regions: the bridge is geofenced, and a few')
    L.append('-- checks sit on the near side of it. Which ones depends on the')
    L.append('-- difficulty and on what the player STARTED with -- a seed')
    L.append('-- beginning at 25 sandwiches needs five more for the check at')
    L.append('-- 30, and nine are reachable, so that check is not gated.')
    L.append('function reachTazland(kind, threshold)')
    L.append('  -- Open only. region_of splits Tazland for "open" and nothing')
    L.append('  -- else, because in Linear there is no unlock to be short of:')
    L.append('  -- the level is gated on posters and the boss before it, and')
    L.append('  -- once that opens the whole level opens with it.')
    L.append('  if linear() then return reach(TAZLAND) end')
    L.append('  local t = tonumber(threshold) or 0')
    L.append('  local pre = TAZLAND_PRE[sd()["difficulty"] or "standard"]')
    L.append('             or TAZLAND_PRE.standard')
    L.append('  if kind == "s" then')
    L.append('    if t <= (tonumber(sd()["starting_sandwiches"]) or 0) + pre.s then')
    L.append('      return 1')
    L.append('    end')
    L.append('  elseif t <= pre.d then')
    L.append('    return 1')
    L.append('  end')
    L.append('  return reach(TAZLAND)')
    L.append('end')
    L.append('')
    L.append('-- One counting section a level, instead of a hundred pins.')
    L.append('function sandwichCounterVisible()')
    L.append('  return (SAND_STEP == 1) and 1 or 0')
    L.append('end')
    L.append('')
    L.append('function destructionCounterVisible()')
    L.append('  return (DEST_STEP == 1) and 1 or 0')
    L.append('end')
    L.append('')
    L.append('-- A boss carries a second check holding a Hindenbird Ticket,')
    L.append('-- but only when bosses are part of the goal -- otherwise the')
    L.append('-- tickets mean nothing and the boss is one location. Linear')
    L.append('-- has no ticket checks at all: with_tickets is open-only.')
    L.append('function ticketVisible()')
    L.append('  if linear() then return 0 end')
    L.append('  return on(sd()["goal_bosses_enabled"]) and 1 or 0')
    L.append('end')
    L.append('')
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True, help="the PopTracker pack root")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    D = load_world()
    loc_path = os.path.join(args.pack, "locations", "locations.json")
    doc = json.load(open(loc_path, encoding="utf-8"))

    changes = transform(doc, D)
    sections, counter_paths = emit_sections(doc)

    # Every sandwich and destruction location the world can emit, mapped to
    # its level's counter. At an interval of 1 that is a hundred a level, only
    # twenty of which have a pin of their own -- so keying this off the pins
    # would lose four fifths of them.
    counters = {}
    for kind, locs in (("s", D.sandwich_locations(1, 0)),
                       ("d", D.destruction_locations(1, "expert"))):
        for loc in locs:
            paths = counter_paths.get((kind, D.LEVEL_NAME[loc["level"]]))
            if paths:
                counters[loc["name"]] = paths

    print(f"    {len(changes)} change(s):")
    for c in changes[:12]:
        print("      " + c)
    if len(changes) > 12:
        print(f"      ... and {len(changes) - 12} more")
    print(f"    {len(sections)} sections, {len(counters)} threshold names "
          f"feeding {len(counter_paths)} counters")

    # Drift is what put the pack in this state, so it is checked rather than
    # assumed: every location any seed could contain must have somewhere to go.
    return _finish(args, doc, D, sections, counters, loc_path)


def _coverage(D, sections, counters):
    """Location names the tracker cannot show. Empty is the only good answer."""
    try:
        # Keys stay STRINGS. catcher_locations indexes this dict with the
        # keys the JSON has, so helpfully converting them to ints returns an
        # empty list and every catcher silently drops out of the sweep.
        catchers = json.load(open(os.path.join(
            WORLD, "data", "taz_catchers.json"), encoding="utf-8"))
    except Exception:
        catchers = {}
    every = D.all_locations(sandwich_interval=1, destruction_interval=1,
                            difficulty="expert", catchers=catchers,
                            with_tickets=True)
    missing = [l["name"] for l in every
               if l["name"] not in sections and l["name"] not in counters]
    return every, missing


def _finish(args, doc, D, sections, counters, loc_path):
    every, missing = _coverage(D, sections, counters)
    print(f"    coverage: {len(every)} possible locations, "
          f"{len(missing)} with nowhere to show")
    for m in missing[:10]:
        print(f"      MISSING  {m}")
    if len(missing) > 10:
        print(f"      ... and {len(missing) - 10} more")

    if args.dry_run:
        print("    dry run; nothing written")
        return 1 if missing else 0

    with open(loc_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")

    head = [
        "AUTO-GENERATED by taz_tracker_gen.py. Do not edit by hand.",
        "",
        "Archipelago location name -> the sections that show it.",
        "",
        "A location appears twice: once on the main map's grouped pin and",
        "once on its own level map. Both are cleared together, which is why",
        "this maps to a list rather than a single path.",
    ]
    body = [lua_table("AP_SECTIONS", sections, head), "",
            lua_table("AP_COUNTERS", counters, [
                "Sandwich locations -> the per-level counter they decrement.",
                "Only used when Sandwich Checks is 1, where the per-threshold",
                "pins are hidden and this is what the player actually sees."])]
    with open(os.path.join(args.pack, "scripts", "ap_sections.lua"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(body) + "\n")

    with open(os.path.join(args.pack, "scripts", "ap_logic.lua"), "w",
              encoding="utf-8") as fh:
        fh.write(emit_logic_lua(D))
    print("    wrote locations.json, ap_sections.lua, ap_logic.lua")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
