"""
taz_dl_test.py -- check DeathLink both ways, and try writing a state.

Two jobs that both hang off the state field we just corrected:

    send      watch what the client would report as you die. Every death
              now comes from a state rather than a position jump, so a warp
              should register nothing at all.

    receive   apply an incoming DeathLink to yourself -- teleport to the
              level start, or lose the boss fight if you are in an arena.

    poke      write a state and see whether the game acts on it. This is how
              pepper and gum are applied, so if Shocked never worked, this is
              where it will show.

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_dl_test.py send
    venv\\Scripts\\python.exe taz_dl_test.py receive
    venv\\Scripts\\python.exe taz_dl_test.py poke 0x10
    venv\\Scripts\\python.exe taz_dl_test.py poke 0x10 --anim 0x1D
"""

import argparse
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run:  venv\\Scripts\\python.exe taz_dl_test.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
    from worlds.tazwanted import _imports
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

STATES = {0x00: "zero", 0x0A: "idle", 0x0C: "spin 1", 0x0D: "spin 2",
          0x10: "shocked", 0x2C: "drown", 0x2D: "FALL", 0x3A: "gum",
          0x3B: "pepper", 0x3D: "0x3D?", 0x3E: "CRUSHED",
          0x59: "CAUGHT", 0x5A: "0x5A?"}
ANIM_OFF = 0x0B8


def connect():
    print(f"\n  world loaded from: {G.__file__}")
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True
    g.refresh_save_file()
    print(f"  state chain: [[TAZ_PTR] + {G.O_STATE_PTR:#x}] + {G.S_STATE:#x}")

    # The offsets were corrected after a live comparison, and a stale install
    # is easy to miss: a built .apworld in custom_worlds takes precedence over
    # worlds/tazwanted. Reading the old offset does not fail -- it returns the
    # low byte of a pointer, which climbs in steps of eight and looks just
    # enough like a state to waste an afternoon.
    if (G.O_STATE_PTR, G.S_STATE) != (0x1C8, 0x0B0):
        print(f"\n  *** THIS COPY IS OUT OF DATE ***")
        print(f"  It expects [[TAZ_PTR] + 0x1C8] + 0xB0, and has "
              f"{G.O_STATE_PTR:#x} / {G.S_STATE:#x}.")
        print(f"  Update the copy named above, and delete any stale")
        print(f"  tazwanted.apworld in custom_worlds.\n")
        sys.exit(1)

    obj = state_addr()
    if obj is not None:
        v = G.mem.read_u8(obj + G.S_STATE)
        print(f"  reading it now: 0x{v:02X} "
              f"({STATES.get(v, 'unrecognised')})")
        if v not in STATES:
            print(f"  That is not a state we know. Values climbing in steps")
            print(f"  of eight are a pointer, which means a wrong offset.")
    print()
    return g


def state_addr():
    try:
        taz = G.mem.read_u32(G.TAZ_PTR)
        if not G.mem.valid_ptr(taz):
            return None
        obj = G.mem.read_u32(taz + G.O_STATE_PTR)
        return obj if G.mem.valid_ptr(obj) else None
    except Exception:
        return None


def cmd_send(g, secs):
    print(f"  Watching for {secs:.0f}s. Die every way you can, then use an")
    print(f"  in-level warp and a hub door -- neither should report.\n")
    print(f"  In a boss arena the helmet counters are shown too.\n")
    print(f"  {'time':>6}  {'state':<10}  {'level':<24}  reports")
    last = None
    last_helm = None   # ((0x08, 0x0C), object address)
    tally = {}
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.03)
        st = g._state()
        kind = g.death_tick()

        # Losing a boss is a separate check -- it reads the arena's own score
        # rather than a state, so death_tick never sees it. Leaving it out of
        # this tool made a boss loss look undetected when it simply was not
        # being asked about.
        if g.boss_lost():
            kind = "boss loss"

        # Show the helmet counters as they move, so the losing moment can be
        # seen even if the rule misses it.
        h = g._helmet_obj()
        if h is not None:
            try:
                pair = (G.mem.read_u8(h + 0x08), G.mem.read_u8(h + 0x0C))
            except Exception:
                pair = None
            # The object ADDRESS is shown too. A helmet that reallocates
            # looks exactly like one whose counters jumped, and the two want
            # very different fixes -- 0xFF,0x00 becoming 0x08,0x09 was a new
            # allocation, not Taz healing.
            if pair and (pair, h) != last_helm:
                moved = last_helm is not None and last_helm[1] != h
                last_helm = (pair, h)
                print(f"  {time.time() - t0:>6.1f}  helmet      "
                      f"+0x08 = 0x{pair[0]:02X}   +0x0C = 0x{pair[1]:02X}"
                      f"   obj 0x{h:08X}"
                      + ("   <-- the object MOVED" if moved else ""))

        if st != last or kind:
            lid = g.level_id()
            where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
            name = STATES.get(st, f"0x{st:02X}" if st is not None else "?")
            if kind:
                tally[kind] = tally.get(kind, 0) + 1
            print(f"  {time.time() - t0:>6.1f}  {name:<10}  "
                  f"{str(where):<24}  {kind or ''}")
            last = st
    print(f"\n  {tally.get('captures', 0)} capture(s), "
          f"{tally.get('void_out', 0)} void(s), "
          f"{tally.get('boss loss', 0)} boss loss(es)")
    print("  A warp that reported nothing is the fix working.\n")


def cmd_receive(g):
    lid = g.level_id()
    where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
    print(f"  In {where}.")
    if g.is_boss():
        print(f"  A boss arena, so an incoming death means losing the fight.")
        until = g.start_boss_loss()
        if not until:
            print("  ...but nothing was applied. That boss may not be set up.\n")
            return
        print(f"  Applied. Holding it for "
              f"{until - time.time():.0f}s -- watch the fight resolve.\n")
        while time.time() < until:
            time.sleep(0.05)
            g.hold_boss_loss()
        print("  Done.\n")
        return
    spawns = _imports.data("taz_spawns.json") or {}
    rec = spawns.get(str(lid))
    if not rec:
        print(f"  No spawn recorded for this level, so nothing to do.\n")
        return
    print(f"  Teleporting to the level start {tuple(rec['pos'])} ...")
    print(f"  {'worked' if g.teleport_to(rec['pos']) else 'FAILED'}\n")


def cmd_poke(g, value, anim, hold):
    """Write a state and watch whether the game acts on it.

    Pepper and gum are applied exactly this way, so a state that does nothing
    when written is a state the game does not drive from this field -- which
    would explain a powerup that never worked however its flag was set.
    """
    obj = state_addr()
    if obj is None:
        sys.exit("  Taz's state object is not there -- load a level first.")
    before = G.mem.read_u8(obj + G.S_STATE)
    print(f"  state is 0x{before:02X} ({STATES.get(before, 'unknown')})")
    print(f"  writing 0x{value:02X} ({STATES.get(value, 'unknown')})"
          + (f" and animation 0x{anim:02X}" if anim is not None else "")
          + f", holding {hold:.1f}s\n")

    end = time.time() + hold
    while time.time() < end:
        try:
            G.mem.write_u8(obj + G.S_STATE, value)
            if anim is not None:
                G.mem.write_u8(obj + ANIM_OFF, anim)
        except Exception as exc:
            sys.exit(f"  the write failed: {exc}")
        time.sleep(0.02)

    after = G.mem.read_u8(obj + G.S_STATE)
    print(f"  state is now 0x{after:02X}")
    if after != value:
        print("  The game overwrote it, which means it is driving this field")
        print("  rather than reading it. A powerup would need its own flag set")
        print("  as well, the way pepper does.")
    else:
        print("  It held. If nothing happened on screen, the state alone is")
        print("  not enough and the matching flag is needed too.")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("send", "receive", "poke"))
    ap.add_argument("value", nargs="?", default="0x10")
    ap.add_argument("--anim", default=None)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--secs", type=float, default=300.0)
    a = ap.parse_args()

    g = connect()
    if a.cmd == "send":
        cmd_send(g, a.secs)
    elif a.cmd == "receive":
        cmd_receive(g)
    else:
        cmd_poke(g, int(a.value, 0),
                 int(a.anim, 0) if a.anim else None, a.hold)


if __name__ == "__main__":
    main()
