#!/usr/bin/env python3
"""The notification gate, offline.

    py -3.13 taz_notify_test.py

Loads the SHIPPED notify.py with pcsx2_mem stubbed out, so this tests the
file the client actually imports rather than a copy of it -- the same trick
taz_goal_test.py uses on TazClient. No emulator.

What it covers:

  * idle() refuses for each of its four reasons, one at a time
  * slowed() on the time scale, on the banner state, and on the settle
  * slowed() is False when the reads fail, not True -- a dead connection
    must not read as a permanent slowdown and mute the client forever
  * the Notifier HOLDS its queue through a slowdown and flushes after,
    in order, losing nothing
  * a source check that idle() still consults slowed(), so the gate cannot
    quietly drift back out
  * the addresses against ee_dump.bin, when one is present
"""

import importlib.util
import os
import struct
import sys
import types

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, wanted {want!r}")


# ------------------------------------------------------------- fake memory

class FakeMem:
    """Just enough of pcsx2_mem to run the gate. Words only; anything not
    written reads as zero, which is what an idle game looks like."""

    def __init__(self):
        self.words = {}
        self.fail = False

    def _get(self, a):
        if self.fail:
            raise RuntimeError("not connected")
        return self.words.get(a, 0)

    def read_u32(self, a):
        return self._get(a)

    def read_float(self, a):
        return struct.unpack("<f", struct.pack("<I", self._get(a)))[0]

    def write_u32(self, a, v):
        self.words[a] = v & 0xFFFFFFFF

    def write_float(self, a, v):
        self.words[a] = struct.unpack("<I", struct.pack("<f", v))[0]

    def write_bytes(self, a, b):
        for i in range(0, len(b) - 3, 4):
            self.words[a + i] = int.from_bytes(b[i:i + 4], "little")

    def read_bytes(self, a, n):
        return b"".join(self._get(a + i).to_bytes(4, "little")
                        for i in range(0, n, 4))[:n]

    def read_u8(self, a):
        return self._get(a & ~3) >> (8 * (a & 3)) & 0xFF

    def write_u8(self, a, v):
        pass


def load_notify(mem):
    """The real notify.py, with `mem` as its pcsx2_mem."""
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    pkg.pcsx2_mem = mem
    sys.modules["tazworld"] = pkg
    sys.modules["tazworld.pcsx2_mem"] = mem
    path = os.path.join(WORLD, "notify.py")
    spec = importlib.util.spec_from_file_location("tazworld.notify", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tazworld.notify"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ tests

def test_idle(N, mem):
    print("\nidle(), one refusal at a time")
    mem.words.clear()
    N._slow_seen = 0.0
    mem.write_float(N.TIME_SCALE, 1.0)
    check("clean game -> a message may go up", N.idle(), True)

    mem.write_u32(N.CTRL + N.C_REQUEST, 1)
    check("a request already pending -> no", N.idle(), False)
    mem.write_u32(N.CTRL + N.C_REQUEST, 0)

    mem.write_u32(N.LIST_A + N.LIST_COUNT, 1)
    check("something already on list A -> no", N.idle(), False)
    mem.write_u32(N.LIST_A + N.LIST_COUNT, 0)

    mem.write_u32(N.SLOTS[N.MSG_FLAGS] + N.SLOT_OPEN, 1)
    check("the slot is open -> no", N.idle(), False)
    mem.write_u32(N.SLOTS[N.MSG_FLAGS] + N.SLOT_OPEN, 0)

    check("and back to yes once all are clear", N.idle(), True)


def test_slowed(N, mem):
    print("\nslowed(), on each signal")
    mem.words.clear()
    N._slow_seen = 0.0
    t = 1000.0

    mem.write_float(N.TIME_SCALE, 1.0)
    check("normal speed", N.slowed(t), False)

    # The banner's own numbers: 0.25 on raise, decaying to a 0.1 floor.
    mem.write_float(N.TIME_SCALE, 0.25)
    check("bounty banner, 0.25", N.slowed(t), True)
    mem.write_float(N.TIME_SCALE, 0.1)
    check("... decayed to its 0.1 floor", N.slowed(t), True)

    # WestBoss.cpp, 0x00190240.
    mem.write_float(N.TIME_SCALE, 0.5)
    check("West boss, 0.5", N.slowed(t), True)

    # The banner outlives the ramp, so the state word alone must hold.
    mem.write_float(N.TIME_SCALE, 1.0)
    mem.write_u32(N.POPUP_STATE, 5)
    check("scale back to 1.0 but banner still up", N.slowed(t), True)
    mem.write_u32(N.POPUP_STATE, 0)

    # Float noise must not read as a slowdown.
    mem.write_float(N.TIME_SCALE, 0.9999)
    check("0.9999 is not a slowdown", N.slowed(t + 10.0), False)


def test_settle(N, mem):
    print("\nslowed(), the settle after it ends")
    mem.words.clear()
    N._slow_seen = 0.0
    t = 2000.0
    mem.write_float(N.TIME_SCALE, 0.25)
    check("slowdown running", N.slowed(t), True)

    mem.write_float(N.TIME_SCALE, 1.0)
    check("scale restored, still settling",
          N.slowed(t + N.SLOW_SETTLE / 2), True)
    check("settle elapsed -> clear",
          N.slowed(t + N.SLOW_SETTLE + 0.01), False)

    # And idle() has to honour it, not just slowed().
    N._slow_seen = 0.0
    mem.write_float(N.TIME_SCALE, 0.25)
    check("idle() refuses during a slowdown", N.idle(), False)
    mem.write_float(N.TIME_SCALE, 1.0)
    N._slow_seen = 0.0
    check("idle() allows once it is over", N.idle(), True)


def test_read_failure(N, mem):
    print("\nslowed(), when the reads fail")
    mem.words.clear()
    N._slow_seen = 0.0
    mem.fail = True
    check("a dead connection is not a slowdown", N.slowed(3000.0), False)
    check("and idle() refuses anyway", N.idle(), False)
    mem.fail = False


def test_notifier_holds(N, mem):
    print("\nthe Notifier holds its queue, then flushes it")
    mem.words.clear()
    N._slow_seen = 0.0
    mem.write_float(N.TIME_SCALE, 1.0)

    raised = []
    N.installed = lambda: True
    N.ticks = lambda: 1
    N.raise_text = lambda text, *a, **k: (raised.append(text), True)[1]

    n = N.Notifier(mode=N.ALL)
    n.enabled = True
    n.hotkey = types.SimpleNamespace(pressed=lambda: False)
    n.push("first", N.ALWAYS)
    n.push("second", N.ALWAYS)

    mem.write_float(N.TIME_SCALE, 0.25)          # a statue goes off
    for _ in range(5):
        n.tick(True)
    check("nothing raised during the slowdown", raised, [])
    check("and nothing was dropped either", len(n.queue), 2)

    mem.write_float(N.TIME_SCALE, 1.0)
    N._slow_seen = 0.0                            # settle expired
    n.tick(True)
    check("one goes up after it, the oldest first", raised, ["first"])
    n.tick(True)
    check("then the next", raised, ["first", "second"])
    check("queue drained", n.queue, [])


def test_source(N):
    """The completion bugs all had the same shape: a fix in one place, and a
    second path underneath that still did the old thing. A source check is
    what stops this one drifting back."""
    print("\nsource check")
    src = open(os.path.join(WORLD, "notify.py")).read()
    body = src.split("def idle(")[1].split("\ndef ")[0]
    check("idle() consults slowed()", "slowed()" in body, True)
    check("slowed() reads the time scale",
          "TIME_SCALE" in src.split("def slowed(")[1].split("\ndef ")[0], True)
    check("slowed() reads the banner state",
          "POPUP_STATE" in src.split("def slowed(")[1].split("\ndef ")[0], True)
    check("TIME_SCALE is the address the dump gave",
          N.TIME_SCALE, 0x004125CC)
    check("POPUP_STATE is the address the dump gave",
          N.POPUP_STATE, 0x003CA3B4)


def test_dump(N):
    """The instructions those two addresses were read out of."""
    print("\nagainst ee_dump.bin")
    if not os.path.exists(DUMP):
        print("  --    no ee_dump.bin here, skipped")
        return
    d = open(DUMP, "rb").read()

    def word(a):
        return struct.unpack_from("<I", d, a)[0]

    # swc1 $f0, 0x35cc($a0) -- the store that makes 0x004125CC the live scale
    check("0x002C91B8 still writes the time scale", word(0x002C91B8), 0xE48035CC)
    # mul.s $f12, $f0, $f12 -- the frame delta being scaled by it
    check("0x00285A28 still scales the frame delta", word(0x00285A28), 0x460C0302)
    # sw $zero, -0x5c4c($s5) -- the banner clearing its state word
    check("0x002027A4 still clears the banner state", word(0x002027A4), 0xAEA0A3B4)
    # jal 0x2c56e8 -- the 100-sandwich line going through the subtitle system,
    # which is why it is LIST_A's job and not slowed()'s
    check("0x0024C7E4 still raises string 422 as a subtitle",
          word(0x0024C7E4), 0x0C0B15BA)
    check("time scale idles at 1.0",
          struct.unpack_from("<f", d, N.TIME_SCALE)[0], 1.0)
    check("banner state idles at 0", word(N.POPUP_STATE), 0)


def main():
    mem = FakeMem()
    N = load_notify(mem)
    test_idle(N, mem)
    test_slowed(N, mem)
    test_settle(N, mem)
    test_read_failure(N, mem)
    test_notifier_holds(N, mem)
    test_source(N)
    test_dump(N)
    print(f"\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
