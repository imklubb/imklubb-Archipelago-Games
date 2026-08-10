# imklubb's Archipelago worlds

A fork of [Archipelago](https://github.com/ArchipelagoMW/Archipelago) holding
the worlds I maintain. Everything upstream is unchanged; my work lives in
`worlds/`.

## The worlds

| Game | Platform | Version | Guide |
|---|---|---|---|
| **Taz: Wanted** | PS2, via PCSX2 | 1.0.0 | [README](worlds/tazwanted/README.md) · [setup](worlds/tazwanted/docs/setup_en.md) |
| **Toy Story 2** | | | |

## Downloading

You do not need this repository to play. Go to
**[Releases](https://github.com/imklubb/imklubb-Archipelago-Games/releases)**
and download the `.apworld` for the game you want, then double-click it.

Releases are tagged per game, because both worlds ship from here:

- `taz-1.0.0` — Taz: Wanted
- `ts2-2.1.2` — Toy Story 2

So the newest Taz release is the newest tag beginning `taz-`, regardless of
what is marked "Latest".

## Trackers

**Taz: Wanted** has a PopTracker pack with all eleven maps, 195 pins and full
auto-tracking: https://github.com/imklubb/Taz-Wanted-Poptracker

Install it once and PopTracker offers you every future update by itself. The
Taz client also has the same maps built in on its own tab, if you would rather
not run a second program.

## Reporting a bug

[Issues](https://github.com/imklubb/imklubb-Archipelago-Games/issues). Say
which game and which version, and attach the client log from `logs/` if you
have one — it records far more than the client window shows.

## For contributors

`tools/` holds the Taz research and test suites, around fifty scripts. The
`*_test.py` ones run offline with no emulator:

```
python tools/taz_catcher_test.py sim
python tools/taz_powerup_test.py sim
python tools/taz_savefile_test.py
```

Building a release is in [GITHUB_WALKTHROUGH.md](GITHUB_WALKTHROUGH.md).

---

Neither world contains any part of its game. You supply your own disc image.
