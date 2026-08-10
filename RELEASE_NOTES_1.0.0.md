# Taz: Wanted — Archipelago 1.0.0

The first public release. Every number below was read out of the code rather
than written from memory.

---

## The two game modes

**Open** shuffles level access into the multiworld. Every level, boss, costume
and bonus game becomes an item, and you choose what the goal requires: Wanted
Posters, boss defeats, the Hindenbird's own unlock, or any combination of the
three.

**Linear** leaves the game's own progression alone. Levels unlock as they
normally would, but each boss is gated behind a number of Wanted Posters that
you set. Costumes and bonus games are still shuffled.

---

## Locations

**168 at default settings.** The two interval options move that between 128 and
552.

| Kind | Count | Notes |
|---|---:|---|
| Wanted Posters | 70 | Seven per level, ten levels |
| Catchers | 45 | Every zookeeper post, including the three hubs |
| Golden Sam statues | 10 | One per level |
| Level Complete | 10 | One per level |
| Sandwiches | 10 | At the default every-100. Every-5 gives 200 |
| Destruction | 10 | At the default every-50%. Every-5% gives 200 |
| Bonus games | 9 | Every level with one; Tazland has none |
| Boss defeats | 4 | Gossamer, Daffy, Sam twice |
| Boss tickets | +4 | Optional, off by default |

The maximum, at sandwiches every 5, destruction every 5%, Expert and boss
tickets on, is **552 locations**.

### The levels

| Level | Costume it grants |
|---|---|
| Ice Burg | Snowboarder |
| Zooney Tunes | Skater |
| Looney Lagoon | Surfer |
| Looningdale's | DJ |
| Samsonian Museum | Ninja |
| Bank of Samerica | SWAT Officer |
| Taz: Haunted | Werewolf |
| Cartoon Strip-Mine | Adventurer |
| Granny Canyon | Cowboy |
| Tazland A-maze-ment Park | Caveman |

Yosemite Zoo, the first hub, grants the Christmas Reindeer.

### The bosses

Gossamer, Daffy, Sam in Dodge City, Sam in the Disco Volcano, and Tweety on
The Hindenbird.

---

## Items

### Progression

- **10 Level Unlocks**, one per level
- **5 Boss Unlocks** — Elephant Pong, Gladiatoons, Dodge City, Disco Volcano,
  The Hindenbird
- **9 Bonus Game Unlocks**
- **11 Costumes** — Skater, Snowboarder, Surfer, Ninja, DJ, SWAT Officer,
  Cowboy, Werewolf, Adventurer, Caveman, Christmas Reindeer

A locked level reads **LOCKED** on the hub signs. A boss door tells you what it
is still waiting for. Phone booths refuse you until the matching costume
arrives.

### Filler

| Item | Effect |
|---|---|
| Raised Bounty | 5,000 to your bounty, through the game's own award |
| Chili Pepper | The fire-breathing powerup |
| Burp Can | The burp |
| Invisibility | Invisible to zookeepers |
| Bubble Gum | The bubble gum state |

### Traps

Off by default. `trap_percent` replaces that share of the filler rather than
adding to it, so turning traps up never changes how many items exist.

| Trap | Effect |
|---|---|
| Dynamite Trap | Taz eats a stick of dynamite |
| Squash Trap | Flattened for ten seconds |
| Electrocute Trap | Shocked |
| Hiccup Trap | Hiccups |
| No Spinning Trap | No spinning for fifteen seconds |
| Costume Strip Trap | Whatever you are wearing comes off |

Traps that stop Taz land whatever he is doing, spinning included, the same way
the game's own hazards do.

---

## Options

| Option | Default | Range |
|---|---|---|
| `game_mode` | open | open, linear |
| `starting_levels` | 1 | 0 to 5 |
| `goal_conditions` | 0 | posters, bosses, Hindenbird, or a combination |
| `goal_posters` | 50 | 10 to 100 |
| `goal_bosses` | 4 | 1 to 4 |
| `poster_pool_open` | 70 | 10 to 100 |
| `poster_pool_linear` | 70 | 10 to 100 |
| `gate_elephant_pong` | 21 | 1 to 100 |
| `gate_gladiatoons` | 42 | 2 to 100 |
| `gate_dodge_city` | 63 | 3 to 100 |
| `gate_disco_volcano` | 70 | 4 to 100 |
| `sandwich_checks` | 100 | 5, 10, 25, 50, 100 |
| `starting_sandwiches` | 0 | |
| `destruction_checks` | 50 | 5, 10, 25, 50 |
| `difficulty` | standard | standard, advanced, expert |
| `trap_percent` | 0 | 0 to 100 |
| `in_game_text` | progressive | how much the game itself tells you |
| `local_filler` | true | keep filler in your own world |
| `death_link` | false | |
| `death_link_sends` | 0 | which of your deaths send |
| `void_out_amnesty` | 1 | 1 to 5 falls before a DeathLink fires |

**Set the in-game Daffy-culty to match your yaml before starting a file.** Some
checks are unreachable on a lower one. The client warns you, but by then some
may already be gone.

---

## What the client does

- **Auto-tracking** for every location type
- **DeathLink**, with a configurable amnesty for falling out of the world
- **An in-client map tab** with the same eleven maps and 195 pins as the
  PopTracker pack, for players who would rather not run a second program
- **A flight recorder** in `logs/`, including the full reasoning behind every
  catcher decision
- **Save-file safety** — the client is the source of truth, not the save. It
  rebuilds your unlocks from the server on every connect, so save states,
  reloading and reconnecting mid-session are all safe. You cannot lose an item
  by reloading and you cannot send a check twice.
- **In-game text**, using the game's own string table and banner

---

## Known limitations

- **Randomised costumes are not possible.** Each level packages only its own
  costume model, verified on two levels. A costume you have not been given the
  level for cannot be rendered.
- **Raised Bounty is a flat 5,000.** Every copy of a filler item is
  indistinguishable at runtime, so scaling it by where it was found is not
  available.
- **Two routes into bonus games are ungated** — the two-player menu and
  Tournament. Neither is reachable by accident.
- **Boss losses cannot be forced.** The boss has to land the last hit itself.

---

## Acknowledgements

Taz: Wanted is by Blitz Games and Infogrames. This project is not affiliated
with either, or with Warner Bros. It contains no part of the game; you supply
your own disc image.
