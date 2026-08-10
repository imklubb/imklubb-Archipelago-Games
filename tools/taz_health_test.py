#!/usr/bin/env python3
"""The flight recorder, against a scripted emulator.

    py -3.13 taz_health_test.py

No emulator and no Archipelago. The real health.py runs against a fake
memory whose numbers this file decides, and time is a variable rather than
the wall clock, so a twenty-second freeze takes no time to test.

WHY THIS IS WORTH TESTING
-------------------------
A diagnostic is only worth having if it is quiet when things are fine and
loud exactly once when they are not. Both halves fail silently:

  * too eager, and it cries wolf over the game's own slow motion -- a Golden
    Sam Statue runs 5.09s and the West boss holds 0.5 for a whole phase, so
    anything that fires on those trains the player to ignore it;

  * too quiet, and the player reloads a save state and the evidence is gone,
    which is the exact situation this was written for.

So the tests are mostly about the cliffs, and about it never being able to
take the client down with it.
"""

import importlib.util
import os
import sys
import tempfile
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

PASS, FAIL = [], []


def chk(label, got, want):
    (PASS if got == want else FAIL).append((label, got, want))
    print(f"  {'ok  ' if got == want else 'FAIL'} {label:<54} "
          f"{got!r}" + ("" if got == want else f"   expected {want!r}"))


def load_health():
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    path = os.path.join(WORLD, "health.py")
    spec = importlib.util.spec_from_file_location("tazworld.health", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tazworld.health"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeMem:
    """A scripted emulator. Only what health.py reads -- anything else it
    starts reading has to be added here in the same commit, or the feature
    silently does nothing while this file stays green."""

    def __init__(self, H):
        self.H = H
        self.scale = 1.0
        self.gtime = 100.0
        self.dt = 0.0166
        self.state = 1
        self.level = 5
        self.dead = None

    def _guard(self):
        if self.dead:
            raise self.dead

    def read_float(self, a):
        self._guard()
        return {self.H.TIME_SCALE: self.scale,
                self.H.GAME_TIME: self.gtime,
                self.H.FRAME_DT: self.dt}[a]

    def read_u32(self, a):
        self._guard()
        return {self.H.GAME_STATE: self.state,
                self.H.LEVEL_ID: self.level}[a]


def run(h, mem, seconds, t0, step=0.1, advance=True):
    """Play forward, returning everything it said."""
    said, t = [], t0
    for _ in range(int(seconds / step)):
        t += step
        if advance:
            mem.gtime += step
        said += h.tick(now=t)
    return said, t


def main():
    H = load_health()
    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "seed.json")

    print("\n  the log lives in the logs folder, not next to the launcher\n")
    chk("named for the seed", os.path.basename(H.path_for(log)),
        "seed_health.log")
    chk("  ...inside logs/", os.path.dirname(H.path_for(log)), H.LOG_DIR)
    chk("  ...and the seed's own folder is not dragged along",
        os.path.isabs(H.path_for(log)), False)

    print("\n  normal play says nothing\n")
    mem = FakeMem(H)
    h = H.Health(mem, H.path_for(log))
    said, t = run(h, mem, 60.0, 0.0)
    chk("a quiet minute", said, [])

    print("\n  the game's OWN slow motion must not cry wolf\n")
    mem.scale = 0.25                      # the bounty banner
    said, t = run(h, mem, 5.1, t)         # a Golden Sam Statue is 5.09s
    chk("a 5.1s statue slowdown", said, [])
    mem.scale = 0.5                       # the West boss
    said, t = run(h, mem, 8.0, t)
    chk("13s in, still under the limit", said, [])
    mem.scale = 1.0
    said, t = run(h, mem, 1.0, t)
    chk("and it ends quietly", said, [])

    print("\n  ...but a slowdown that never ends is reported once\n")
    mem.scale = 0.1
    said, t = run(h, mem, H.STUCK_SLOWMO + 1.0, t)
    chk("past STUCK_SLOWMO", len(said), 1)
    chk("  it names the symptom", "slow motion" in (said[0] if said else ""),
        True)
    chk("  it tells the player what clears it",
        "save state" in (said[0] if said else ""), True)
    said, t = run(h, mem, 20.0, t)
    chk("  and does not repeat inside REPEAT_EVERY", said, [])

    print("\n  a frozen game -- the clock stops while it says it is running\n")
    mem.scale = 1.0
    h2 = H.Health(FakeMem(H), H.path_for(log))
    m2 = h2.mem
    said, t2 = run(h2, m2, 5.0, 0.0)
    chk("running normally", said, [])
    said, t2 = run(h2, m2, H.FROZEN_AFTER - 0.5, t2, advance=False)
    chk("just under FROZEN_AFTER", said, [])
    said, t2 = run(h2, m2, 1.0, t2, advance=False)
    chk("past it", len(said), 1)
    chk("  it blames the emulator, not the client",
        "PCSX2 is hung" in (said[0] if said else ""), True)

    print("\n  ...but a frozen clock on a LOADING screen is normal\n")
    h3 = H.Health(FakeMem(H), H.path_for(log))
    m3 = h3.mem
    m3.state = 5
    said, _ = run(h3, m3, 30.0, 0.0, advance=False)
    chk("30s of loading", said, [])
    said, _ = run(h3, m3, H.STUCK_LOAD - 28.0, 30.0, advance=False)
    chk("but a load that never finishes is reported", len(said), 1)

    print("\n  losing the emulator\n")
    h4 = H.Health(FakeMem(H), H.path_for(log))
    m4 = h4.mem
    run(h4, m4, 2.0, 0.0)
    m4.dead = OSError("PINE went away")
    said = []
    for i in range(H.LOST_AFTER + 5):
        said += h4.tick(now=100.0 + i)
    chk("reported after LOST_AFTER failures", len(said), 1)
    chk("  and it points at the log file",
        "health log" in (said[0] if said else ""), True)
    m4.dead = None
    said = h4.tick(now=200.0)
    chk("recovery is quiet", said, [])
    chk("  ...and does not claim a freeze it never watched",
        any("clock has not moved" in l for l in said), False)
    chk("  ...but recorded",
        any("recovered" in l for l in h4.lines), True)

    print("\n  a stalled client re-baselines instead of crying freeze\n")
    h8 = H.Health(FakeMem(H), H.path_for(log))
    m8 = h8.mem
    run(h8, m8, 3.0, 0.0)
    said = h8.tick(now=100.0)          # the client was starved for 97s
    chk("a huge tick gap says nothing alarming", said, [])
    chk("  ...but is recorded",
        any("went 97.0s between looks" in l for l in h8.lines), True)
    said = h8.tick(now=100.1)
    chk("  and the freeze timer restarted", said, [])

    print("\n  the log is on disk AS IT HAPPENS, not every twenty seconds\n")
    # This is the property a real PCSX2 crash disproved in the first
    # version: the file stopped 4.5s before the crash, so the run-up -- the
    # only part worth having -- was never written.
    own = os.path.join(tmp, "live_health.log")
    h9 = H.Health(FakeMem(H), own)
    m9 = h9.mem
    h9.tick(now=0.1)
    chk("the file exists after ONE tick", os.path.exists(own), True)
    m9.scale = 0.25
    h9.tick(now=0.2)
    chk("  and a change is readable immediately, with no flush",
        "0.250" in open(own, encoding="utf-8").read(), True)

    # ...and an anomaly, all the way through to disk.
    m9.scale = 0.05
    run(h9, m9, H.STUCK_SLOWMO + 1.0, 0.2)
    body = open(own, encoding="utf-8").read()
    chk("  an anomaly reaches the file", "***" in body, True)
    chk("  with a header a player can read",
        body.startswith("Taz: Wanted -- client health log"), True)
    chk("  and the samples that preceded it", "scale 1.000" in body, True)

    print("\n  the previous session is kept, because that is the crashed one\n")
    h9.close()
    h10 = H.Health(FakeMem(H), own)
    h10.tick(now=0.1)
    prev = own[:-4] + ".prev.log"
    chk("the old log was rotated", os.path.exists(prev), True)
    chk("  and it still has the anomaly",
        "***" in open(prev, encoding="utf-8").read(), True)
    chk("  while the new one is fresh",
        "***" in open(own, encoding="utf-8").read(), False)
    h10.close()

    print("\n  a cap, so a long session cannot fill a disk\n")
    cap = os.path.join(tmp, "cap_health.log")
    h5 = H.Health(FakeMem(H), cap)
    for i in range(H.MAX_LINES + 500):
        h5.note("line %d" % i)
    chk("samples stop at the cap", h5.written, H.MAX_LINES)
    h5._say("x", "an anomaly after the cap", 1e9)
    chk("  but anomalies still get through", h5.written, H.MAX_LINES + 1)
    h5.close()

    print("\n  it can never take the client down\n")

    class Hostile:
        def read_float(self, a):
            raise RuntimeError("boom")

        def read_u32(self, a):
            raise RuntimeError("boom")

    h6 = H.Health(Hostile(), H.path_for(log))
    try:
        for i in range(5):
            h6.tick(now=float(i))
        ok = True
    except Exception:
        ok = False
    chk("a hostile memory is swallowed", ok, True)

    # /dev/null is a file, so using it as a directory is ENOTDIR -- an
    # unwritable path that stays unwritable even running as root.
    h7 = H.Health(FakeMem(H), os.path.join(os.devnull, "nope.log"))
    chk("an unwritable log path does not raise", h7.flush(), False)
    try:
        h7.tick(now=1.0)
        ok = True
    except Exception:
        ok = False
    chk("  ...and ticking still works", ok, True)
    chk("make() returns None rather than raising on a bad mem",
        H.make(None, log) is not None, True)

    print(f"\n  {len(PASS)}/{len(PASS) + len(FAIL)} passed")
    if FAIL:
        for label, got, want in FAIL:
            print(f"    {label}: got {got!r}, expected {want!r}")
        return 1
    print("  Quiet when it should be, loud once when it should not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
