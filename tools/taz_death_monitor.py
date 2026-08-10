"""
taz_death_monitor.py -- everything that decides whether a death is sent.

Deaths not firing during the Hindenbird fight but firing on the phase change
means something is suppressing them, and there are several candidates. Rather
than guess which, this shows all of them at once, every time anything moves:

    state       what Taz is doing, named where we know the name
    level       where he is, and whether it counts as a boss arena
    settle      the load window -- nothing is trusted while this is counting
    armed       the helmet's last-hit flag, which arms a boss loss
    verdict     what death_tick and boss_lost actually return

Run it next to Launcher.py with the AP client CLOSED:

    venv\\Scripts\\python.exe taz_death_monitor.py
    venv\\Scripts\\python.exe taz_death_monitor.py --all   every tick, not
                                                          only on change
"""

import argparse
import sys
import time

try:
    import yaml  # noqa: F401
except ImportError:
    print(f"\n  Wrong Python: {sys.executable}")
    print("  Run:  venv\\Scripts\\python.exe taz_death_monitor.py\n")
    sys.exit(1)

try:
    from worlds.tazwanted import game as G
    from worlds.tazwanted import logic as L
except Exception as exc:
    sys.exit(f"could not import the world: {exc}\n"
             f"  Run this from the Archipelago folder, next to Launcher.py.")

STATES = {0x00: "zero", 0x0A: "idle", 0x0C: "spin 1", 0x0D: "spin 2",
          0x0E: "spin 3", 0x10: "shocked", 0x15: "DC damaged",
          0x2C: "DROWN", 0x2D: "fall", 0x3A: "gum", 0x3B: "pepper",
          0x3D: "VOID", 0x3E: "CRUSHED", 0x4F: "tnt",
          0x54: "caught 2", 0x55: "caught 3", 0x59: "CAUGHT",
          0x5A: "BOSS LOSS"}


def neighbourhood(g, span=0x40, secs=600.0):
    """Watch the bytes around Daffy's score, live.

    Daffy's chain is confirmed, so his address is known every load even though
    it moves. His score sits in a run like "02 03 03" -- a structure, not a
    lone byte -- and Taz's is very likely in the same one.

    Anything that changes is printed with its offset from Daffy, because an
    offset survives a reload and an address does not.
    """
    import time

    # Resolved here rather than through the world, so the tool works against
    # any installed version -- depending on a method the world only gained in
    # v97 made this fail with a traceback instead of a message.
    DAFFY_PTR, DAFFY_OFF = 0x003FF064, 0x678C

    def daffy_addr():
        try:
            p = G.mem.read_u32(DAFFY_PTR)
        except Exception:
            return None
        return (p + DAFFY_OFF) if G.mem.valid_ptr(p) else None
    first = daffy_addr()
    print(f"\n  Daffy's score is at 0x{first:08X} this load."
          if first else
          "\n  Cannot resolve Daffy's score -- is the fight loaded?")
    if first is None:
        return
    print(f"  Watching {span} bytes around it.")
    print(f"  Score for BOTH sides and see which offsets move.\n")
    last = None
    while secs > 0:
        time.sleep(0.1)
        secs -= 0.1
        base = daffy_addr()
        if base is None:
            continue
        lo = base - span // 2
        try:
            raw = G.mem.read_bytes(lo, span)
        except Exception:
            continue
        if last is not None and raw != last:
            for i in range(span):
                if raw[i] != last[i]:
                    off = lo + i - base
                    tag = "  <-- DAFFY" if off == 0 else ""
                    print(f"    Daffy {off:+#06x}   "
                          f"{last[i]:3d} -> {raw[i]:3d}{tag}")
            print()
        last = raw
    print("  An offset that moves only when TAZ scores is his.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="print every tick, not only on change")
    ap.add_argument("--secs", type=float, default=900.0)
    ap.add_argument("--near-daffy", action="store_true",
                    help="watch the bytes around Daffy's score instead")
    a = ap.parse_args()

    print(f"\n  world loaded from: {G.__file__}")
    if not G.mem or not G.mem.hook():
        sys.exit("could not reach PCSX2 -- is the AP client still open?")
    g = G.Game()
    g.connected = True
    g.refresh_save_file()

    if a.near_daffy:
        neighbourhood(g, secs=a.secs)
        return

    print(f"  state chain [[TAZ_PTR] + {G.O_STATE_PTR:#x}] + {G.S_STATE:#x}")
    print(f"  watching for {a.secs:.0f}s. Fight the boss, die a few ways.\n")
    print(f"  {'time':>6} {'state':<11} {'level':<22} {'settle':>6} "
          f"{'helmet':>12} verdict")

    last = None
    tally = {}
    t0 = time.time()
    while time.time() - t0 < a.secs:
        time.sleep(0.03)
        lid = g.level_id()
        where = L.LEVEL_NAME.get(lid) or L.LEVEL_IDS.get(lid, lid)
        st = g._state()
        name = STATES.get(st, f"0x{st:02X}" if st is not None else "?")

        settle = max(0.0, getattr(g, "_settled_at", 0.0) - time.time())
        helm = ""
        # Gladiatoons is decided by a clock and two scores rather than a
        # helmet, so it shows those in the same column.
        if lid == 12:
            try:
                import struct as _s
                t = _s.unpack("<f", G.mem.read_bytes(0x00380E28, 4))[0]
                # Through the pointer, because the scores move every load.
                p = G.mem.read_u32(0x003FF064)
                ea = (p + 0x678C) if G.mem.valid_ptr(p) else None
                enemy = G.mem.read_u8(ea) if ea else "?"
                ours = "?"
                helm = (f"{t:6.1f}s {ours}-{enemy}"
                        + (" over" if getattr(g, "_fight_over", False)
                           else " started"
                           if getattr(g, "_timer_started", False)
                           else " WAITING"))
            except Exception:
                helm = "?"
        h = g._helmet_obj() if (lid != 12 and hasattr(g, "_helmet_obj")) \
            else None
        if h is not None:
            try:
                helm = (f"{G.mem.read_u8(h + 0x08):02X}/"
                        f"{G.mem.read_u8(h + 0x0C):02X}"
                        f"{' armed' if getattr(g, '_armed', False) else ''}")
            except Exception:
                helm = "?"

        # Ask both detectors, exactly as the client does.
        kind = g.death_tick()
        boss = g.boss_lost()
        verdict = kind or ("boss loss" if boss else "")
        if verdict:
            tally[verdict] = tally.get(verdict, 0) + 1

        row = (name, str(where), round(settle), helm, verdict)
        if a.all or row != last or verdict:
            last = row
            flag = ""
            if settle > 0 and st in (0x2C, 0x2D, 0x3D, 0x3E, 0x59, 0x5A):
                flag = "   <-- a death, but the load window is suppressing it"
            print(f"  {time.time() - t0:>6.1f} {name:<11} {str(where):<22} "
                  f"{settle:>5.1f}s {helm:>22} {verdict}{flag}")

    print(f"\n  {tally}")
    print("  A death state that showed nothing in the verdict column is the")
    print("  one to explain -- the flag on that row usually says why.\n")


if __name__ == "__main__":
    main()
