# imklubb's Archipelago worlds

A fork of [Archipelago](https://github.com/ArchipelagoMW/Archipelago) holding
the source for the worlds I maintain. Everything upstream is unchanged; my work
lives in `worlds/`.

**This repository is the source. It is not where you download anything.**

## Downloads

| Game | Get the apworld | Tracker |
|---|---|---|
| **Taz: Wanted** | [Archipelago-Taz-Wanted](https://github.com/imklubb/Archipelago-Taz-Wanted/releases/latest) | [Taz-Wanted-Poptracker](https://github.com/imklubb/Taz-Wanted-Poptracker) |
| **Toy Story 2** | [Archipelago-Toy-Story-2](https://github.com/imklubb/Archipelago-Toy-Story-2/releases/latest) | [Toy-Story-2-Poptracker](https://github.com/imklubb/Toy-Story-2-Poptracker/releases/latest) |

Each of those has its own setup guide and its own issue tracker. Bug reports
belong there, next to the version they are about.

## What is in here

```
worlds/tazwanted/     the Taz: Wanted world
worlds/toystory2/     the Toy Story 2 world
tools/                Taz research tools and test suites, ~50 scripts
```

## Running the Taz tests

All offline. No emulator, no ROM, no save file.

```
python tools/taz_catcher_test.py sim
python tools/taz_powerup_test.py sim
python tools/taz_savefile_test.py
python tools/taz_sandwich_test.py
python tools/taz_health_test.py
python tools/taz_notify_test.py
python tools/taz_door_test.py
python tools/taz_goal_test.py
python tools/taz_pool_test.py
python tools/taz_coaster_test.py sim
python tools/taz_nospin_test.py sim
```

Several of the non-test tools have a `check` verb that verifies their
assumptions about the game's memory against a RAM dump, also with nothing
running:

```
python tools/taz_enemylist.py check
python tools/taz_trap.py check
python tools/taz_secrets.py check
```

Take a dump first with `python tools/taz_ramdump.py --out ee_dump.bin`.

## Building the Taz apworld

```
python tools/build_apworld.py
```

Writes `dist/tazwanted.apworld` and then checks it: one top-level folder, no
bytecode, and the version inside matching `worlds/tazwanted/archipelago.json`.

Release steps are in [GITHUB_WALKTHROUGH.md](GITHUB_WALKTHROUGH.md).

---

Neither world contains any part of its game. You supply your own disc image.
