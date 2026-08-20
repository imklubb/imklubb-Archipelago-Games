# Taz: Wanted — Archipelago

An [Archipelago](https://archipelago.gg) randomizer for **Taz: Wanted** (PS2),
played on PCSX2. Level access, costumes, bonus games and boss fights all become
multiworld items, and 168 things you do in the game become checks.

Version 1.0.0. See [RELEASE_NOTES_1.0.0.md](RELEASE_NOTES_1.0.0.md) for the
full breakdown of what is randomized, and
[GITHUB_WALKTHROUGH.md](GITHUB_WALKTHROUGH.md) for how releases are cut.

---

## Set-Up

The full guide is in [worlds/tazwanted/docs/setup_en.md](worlds/tazwanted/docs/setup_en.md),
and it is the same page Archipelago shows in its own setup list.

---

## What you need

| | |
|---|---|
| Archipelago | 0.6.4 or newer |
| PCSX2 | 2.0 or newer, with PINE enabled |
| The game | Taz: Wanted (PS2), `SLUS-20236` |
| Tracker (optional) | [PopTracker](https://github.com/black-sliver/PopTracker) and the [Taz Wanted pack](https://github.com/imklubb/Taz-Wanted-Poptracker) |

The apworld does not contain, and will never contain, any part of the game.
You supply your own disc image.

---

## The tracker

There is a PopTracker pack with all eleven maps, 195 pins and full
auto-tracking, in its own repository:

**https://github.com/imklubb/Taz-Wanted-Poptracker**

Install it once and PopTracker will offer you every future update
automatically.

If you would rather not run a second program, the client has the same maps and
pins built in, on its own tab.

---

## Client commands

Typed into the Taz Wanted Client:

| Command | What it does |
|---|---|
| `/taz` | Mode, locations sent, unlocks, and what the goal still needs |
| `/goal` | In Open, each goal condition and its progress. In Linear, every poster gate and which are open |
| `/difficulty` | What the game is set to versus what your yaml expects |
| `/deathlink` | Turn DeathLink on or off, overriding the yaml |
| `/resync` | Rebuild everything from the server's item list |

---

## If something goes wrong

**A check did not send.** The client keeps a log in `logs/`. Catcher decisions
in particular are written there in full, with the reasoning for each one.

**The client says your difficulty is wrong.** It is. Some checks are
unreachable on a lower Daffy-culty than your yaml assumed, so fix it before
playing rather than after.

**Nothing connects.** Only one program can hold the PINE socket. Close anything
else that talks to PCSX2.

Bug reports and questions: [Issues](https://github.com/imklubb/imklubb-Archipelago-Games/issues).

---

## Where this lives

This world ships from **imklubb-Archipelago-Games**, a fork of Archipelago that
also holds the Toy Story 2 world. Releases are tagged per game, so a Taz
release is `taz-1.0.0` and a Toy Story 2 one is `ts2-2.1.2`.

## Building it yourself

`tazwanted.apworld` is a zip of `worlds/tazwanted/`. To build it:

```
python tools/build_apworld.py
```

That writes `dist/tazwanted.apworld`. There is nothing to compile.

## Running the tests

Everything is testable without the emulator. From the repository root:

```
python tools/taz_catcher_test.py sim
python tools/taz_powerup_test.py sim
python tools/taz_savefile_test.py
```

`tools/` holds around fifty scripts. The `*_test.py` ones are offline suites;
the rest are research and diagnostic tools that talk to a running PCSX2 over
PINE. Several have a `check` verb that verifies their assumptions against a RAM
dump with no emulator running at all.

---

## Credits

Written by **imklubb**. Taz: Wanted is by Blitz Games and Infogrames. This
project is not affiliated with either, or with Warner Bros.
