#!/usr/bin/env python3
"""taz_enemylist.py -- the enemy list is a LINKED LIST, not an array.

WHAT THIS IS FOR
----------------
`game.py` has always read the enemies as

    n = read_u32(0x0046C720)                    # "ENEMY_COUNT"
    for i in range(n):
        ptr = read_u32(0x0046C680 + i * 4)      # "ENEMY_ARRAY[i]"

0x0046C680 is not an array. It is the `next`/`prev` pair of a circular
doubly-linked list sentinel that lives at 0x0046C510:

    0x0046C510  the list head
       +0x170 -> 0x0046C680     head->next   read as "ENEMY_ARRAY[0]"
       +0x174 -> 0x0046C684     head->prev   read as "ENEMY_ARRAY[1]"
       +0x210 -> 0x0046C720     node count   read as "ENEMY_COUNT"

So index 0 is the FIRST enemy, index 1 is the LAST enemy, and index 2 and up
are `head+0x178` onwards -- other fields of the sentinel, permanently zero and
dropped by valid_ptr without a word. The count is correct. The indexing is not.

**The client can only ever see two enemies.** Everything in the middle of the
list is invisible, and has been for the whole project.

That is the Zooney Tunes bee hive catcher. keeper05 is the only keeper in
level 5 with two other enemies inside the 3000-unit activation radius
(brownbear01 at 572, brownbear02 at 1355). With three enemies active you can
reach the newest and the oldest; keeper05 is the one in the middle. Its
defeated flag was never flickering -- it was never being read.

THE STRUCTURE, verified
-----------------------
A level manager at 0x0046C4E0 (its first bytes are the level name ASCII, which
is why that address was already known as LEVEL_ID_ASCII -- it reads "safari").
It holds ELEVEN group heads at +0x30, stride 0x220, built by Group::Init at
0x0023CA30. All eleven close on themselves in the dump and every +0x210 equals
its walked length exactly: 0, 11, 378, 67, 0, 129, 0, 0, 21, 0, 4.

Enemies live in a PAIR of those groups and are shuttled between them:

    group 0   0x0046C510   ACTIVE    within 3000 units of Taz
    group 1   0x0046C730   DORMANT   everything else, including the beaten

Each enemy caches both in its own sub-object -- SUB+0xD0 is its active group,
SUB+0xD4 its dormant one -- so the tool never has to guess which is which.

  0x0023CD40  AddChild      push-FRONT, then count++ at +0x210
  0x0023CD70  RemoveChild   pure unlink, then count--. No slot is ever written,
                            nothing is shifted, so there are no holes -- and no
                            indices either.
  0x00162DF8  Enemy_ShouldBeActive: |dy| <= SUB+0x40 and distance < SUB+0x40,
                            and SUB+0x40 is 3000.0 on every enemy measured.
  0x001633BC  state 0  dormant -> active   (remove [SUB+0xD4], add [SUB+0xD0])
  0x00164178  state 14 active -> dormant   (remove [SUB+0xD0], add [SUB+0xD4])

THE DEFEAT BIT
--------------
Because the object is only ever REPARENTED, never freed, it stays readable
forever -- which means there is a permanent defeat flag and no timing window
at all.

    SUB+0x300   1 from GenericAI::Init (0x00160C8C).
                Zeroed at 0x00163E8C and NOWHERE else -- the state 6 -> 14
                handoff, i.e. the defeat that leads to a despawn.
                The state-0 handler refuses to reactivate a zeroed one
                (0x001633CC `beqz`), so it is genuinely permanent.

    SUB+0x0CC   what the judge uses today. NOT a defeat flag -- a hit/stun
                latch. Four setters all store a literal 1 on any hit, and
                every state handler clears it again on the way past. It reads
                1 during "defeated" only incidentally.

USAGE
-----
    py -3.13 taz_enemylist.py check              offline, against ee_dump.bin
    py -3.13 taz_enemylist.py walk               live, one snapshot
    py -3.13 taz_enemylist.py watch [--out F]    live, continuous

`check` needs no emulator. It asserts every constant this file depends on
against the dump and proves the walk finds a keeper the old read cannot, so
the thing is known to work before anyone spends time at PCSX2.
"""

import argparse
import os
import struct
import sys

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

# ---------------------------------------------------------------- constants

MANAGER      = 0x0046C4E0       # level manager; +0x00 is the level name ASCII
GROUP_FIRST  = MANAGER + 0x30   # 0x0046C510
GROUP_STRIDE = 0x220
GROUP_N      = 11

ACTIVE_GROUP  = GROUP_FIRST                     # 0x0046C510
DORMANT_GROUP = GROUP_FIRST + GROUP_STRIDE      # 0x0046C730

L_NEXT  = 0x170
L_PREV  = 0x174
L_OWNER = 0x1E0                 # node -> the group holding it
L_COUNT = 0x210
L_FLAG  = 0x214

# What game.py believes today, kept here so `check` can demonstrate the gap.
OLD_ARRAY = 0x0046C680          # == ACTIVE_GROUP + L_NEXT
OLD_COUNT = 0x0046C720          # == ACTIVE_GROUP + L_COUNT

E_POS, E_NAME, E_TYPE, E_SUB, E_ATYPE = 0x0C0, 0x180, 0x1A0, 0x1D8, 0x1FC
S_HOME, S_ANIM, S_HIT, S_OUTER = 0x030, 0x0B0, 0x0CC, 0x040
S_GRP_ACTIVE, S_GRP_DORMANT = 0x0D0, 0x0D4
S_ALIVE = 0x300                 # 1 = can still act, 0 = beaten for good

EE_MIN, EE_MAX = 0x00100000, 0x02000000

# A walk must terminate even if it reads a torn pointer mid-frame.
WALK_CAP = 4096


def valid_ptr(p):
    return p is not None and EE_MIN <= p < EE_MAX


# ---------------------------------------------------------------- memory

class DumpMem:
    """ee_dump.bin, where file offset == EE address."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)

    def read_u32(self, a):
        if not 0 <= a <= self.size - 4:
            raise ValueError(f"0x{a:08X} outside the dump")
        self.f.seek(a)
        return struct.unpack("<I", self.f.read(4))[0]

    def read_float(self, a):
        self.f.seek(a)
        return struct.unpack("<f", self.f.read(4))[0]

    def read_bytes(self, a, n):
        self.f.seek(a)
        return self.f.read(n)

    def valid_ptr(self, p):
        return valid_ptr(p)


def live_mem():
    """pcsx2_mem, hooked. None with a printed reason if it cannot be."""
    sys.path.insert(0, WORLD)
    try:
        import pcsx2_mem as mem
    except Exception as e:
        print(f"    pcsx2_mem did not import: {type(e).__name__}: {e}")
        print("    It needs pcsx2_interface/pine.py, which lives inside "
              "the world.")
        return None
    try:
        ok = mem.hook()
    except Exception as e:
        print(f"    hooking PCSX2 failed: {type(e).__name__}: {e}")
        return None
    if not ok:
        print("    could not reach PCSX2 on PINE. Is the game running, and "
              "is PINE enabled in Settings -> Advanced (slot 28011)?")
        print("    Close the AP client first -- only one thing at a time on "
              "that socket.")
        return None
    return mem


# ---------------------------------------------------------------- the walk

def walk(mem, head, cap=WALK_CAP):
    """Every node linked into `head`, in list order.

    Order is meaningful: AddChild is push-front, so index 0 is the most
    recently added and the last entry is the oldest. Stops on a bad pointer
    rather than raising -- a torn read mid-frame should cost one sample, not
    the run.
    """
    out = []
    try:
        cur = mem.read_u32(head + L_NEXT)
    except Exception:
        return out
    while cur != head and valid_ptr(cur) and len(out) < cap:
        out.append(cur)
        try:
            cur = mem.read_u32(cur + L_NEXT)
        except Exception:
            break
    return out


def group_count(mem, head):
    try:
        return mem.read_u32(head + L_COUNT)
    except Exception:
        return None


def old_read(mem, head=ACTIVE_GROUP):
    """Exactly what game.py's catchers() does today, so the two can be shown
    side by side. Reproduces the bug on purpose -- do not 'fix' this.

    game.py hardcodes 0x0046C680 and 0x0046C720, which ARE
    ACTIVE_GROUP + 0x170 and ACTIVE_GROUP + 0x210. Written relative to the head
    the same arithmetic applies to any group, which is what lets `check`
    demonstrate the failure against the dormant list -- the only one with
    anything in it in a dump taken away from the enemies. Against
    ACTIVE_GROUP it is byte-for-byte the shipping behaviour.
    """
    out = []
    try:
        n = mem.read_u32(head + L_COUNT)
    except Exception:
        return out
    if not 0 < n <= 40:
        return out
    for i in range(n):
        try:
            ptr = mem.read_u32(head + L_NEXT + i * 4)
        except Exception:
            continue
        if not valid_ptr(ptr):
            continue
        out.append(ptr)
    return out


def _ascii(mem, a, n):
    try:
        return mem.read_bytes(a, n).split(b"\0")[0].decode("ascii", "replace")
    except Exception:
        return ""


def describe(mem, ptr):
    """Everything worth knowing about one enemy. Never raises."""
    d = {"ptr": ptr, "obe": _ascii(mem, ptr + E_TYPE, 24),
         "name": _ascii(mem, ptr + E_NAME, 20)}
    for key, off in (("atype", E_ATYPE), ("owner", L_OWNER), ("sub", E_SUB)):
        try:
            d[key] = mem.read_u32(ptr + off)
        except Exception:
            d[key] = None
    sub = d["sub"]
    for key in ("anim", "hit", "alive", "home", "outer", "g_act", "g_dor"):
        d[key] = None
    if valid_ptr(sub):
        for key, off in (("anim", S_ANIM), ("hit", S_HIT), ("alive", S_ALIVE),
                         ("g_act", S_GRP_ACTIVE), ("g_dor", S_GRP_DORMANT)):
            try:
                d[key] = mem.read_u32(sub + off)
            except Exception:
                pass
        try:
            d["home"] = tuple(mem.read_float(sub + S_HOME + j * 4)
                              for j in range(3))
        except Exception:
            pass
        try:
            d["outer"] = mem.read_float(sub + S_OUTER)
        except Exception:
            pass
    d["keeper"] = d["obe"][:4] in ("keep", "catc")
    return d


def row(i, d):
    home = ("(%8.0f,%8.0f,%8.0f)" % d["home"]) if d["home"] else "(unreadable)"
    return ("  [%2d] %08X %-16s %-18s %-28s anim=%-3s hit=%-4s alive=%s"
            % (i, d["ptr"], d["obe"][:16], d["name"][:18], home,
               d["anim"], d["hit"], d["alive"]))


# ---------------------------------------------------------------- check

class Check:
    def __init__(self):
        self.bad = 0
        self.n = 0

    def __call__(self, ok, what, detail=""):
        self.n += 1
        if not ok:
            self.bad += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}"
              + (f"   {detail}" if detail else ""))
        return ok


def cmd_check(args):
    """Offline. Assert every constant this file rests on, against the dump."""
    path = args.dump or os.path.join(HERE, "ee_dump.bin")
    if not os.path.exists(path):
        print(f"    no dump at {path}. Take one with:")
        print("        py -3.13 taz_ramdump.py --out ee_dump.bin")
        return 2
    mem = DumpMem(path)
    c = Check()

    print(f"\n  {path}  ({mem.size / (1 << 20):.0f}MB, offset == address)\n")

    print("  -- the addresses game.py uses are fields of a list head --")
    c(ACTIVE_GROUP + L_NEXT == OLD_ARRAY,
      "ACTIVE_GROUP + 0x170 == the old ENEMY_ARRAY",
      f"0x{ACTIVE_GROUP + L_NEXT:08X} == 0x{OLD_ARRAY:08X}")
    c(ACTIVE_GROUP + L_COUNT == OLD_COUNT,
      "ACTIVE_GROUP + 0x210 == the old ENEMY_COUNT",
      f"0x{ACTIVE_GROUP + L_COUNT:08X} == 0x{OLD_COUNT:08X}")
    c(DORMANT_GROUP == 0x0046C730, "DORMANT_GROUP is one stride on",
      f"0x{DORMANT_GROUP:08X}")

    print("\n  -- all eleven groups are well-formed circular lists --")
    for k in range(GROUP_N):
        h = GROUP_FIRST + k * GROUP_STRIDE
        nodes = walk(mem, h)
        closed = True
        try:
            cur = mem.read_u32(h + L_NEXT)
            steps = 0
            while cur != h and valid_ptr(cur) and steps <= WALK_CAP:
                cur = mem.read_u32(cur + L_NEXT)
                steps += 1
            closed = (cur == h)
        except Exception:
            closed = False
        n = group_count(mem, h)
        c(closed and n == len(nodes),
          f"group {k:2d} @0x{h:08X}",
          f"count={n} walked={len(nodes)} closes={closed}")

    print("\n  -- the enemies are in the dormant group, and nameable --")
    nodes = walk(mem, DORMANT_GROUP)
    ens = [describe(mem, p) for p in nodes]
    keepers = [d for d in ens if d["keeper"]]
    c(len(ens) > 0, "the dormant group has enemies in it", f"{len(ens)} nodes")
    c(all(d["owner"] == DORMANT_GROUP for d in ens),
      "every node's +0x1E0 back-pointer names its group")
    c(all(d["atype"] == 40 for d in ens),
      "every node reads type 40 at +0x1FC")
    c(len(keepers) >= 1, "keepers are present",
      ", ".join(d["name"].replace("enemy ", "") for d in keepers))
    c(all(d["g_act"] == ACTIVE_GROUP and d["g_dor"] == DORMANT_GROUP
          for d in ens if d["g_act"] is not None),
      "every enemy caches the same group pair at SUB+0xD0 / +0xD4")

    print("\n  -- the activation radius the cull compares against --")
    outers = {d["outer"] for d in ens if d["outer"]}
    c(outers == {3000.0}, "SUB+0x40 is 3000.0 on every enemy",
      f"{sorted(outers)}")

    print("\n  -- the walk sees enemies the old index read cannot --")
    seen_old = set(old_read(mem, DORMANT_GROUP))
    seen_new = set(nodes)
    missed = seen_new - seen_old
    c(len(seen_old) <= 2,
      "the old read can never return more than two nodes",
      f"it returned {len(seen_old)} of {len(seen_new)}")
    c(len(missed) > 0, "the walk recovers the ones it misses",
      f"{len(missed)} recovered")
    missed_keepers = [d for d in ens if d["ptr"] in missed and d["keeper"]]
    c(len(missed_keepers) > 0,
      "including keepers that were invisible to the client",
      ", ".join(d["name"].replace("enemy ", "") for d in missed_keepers))

    print("\n  -- the permanent defeat bit --")
    c(all(d["alive"] in (0, 1) for d in ens if d["alive"] is not None),
      "SUB+0x300 reads 0 or 1 on every enemy",
      f"{sorted({d['alive'] for d in ens})}")

    print(f"\n  {c.n - c.bad}/{c.n} checks passed\n")
    if c.bad:
        print("  Something above does not hold. Do NOT run the live tool "
              "until it does.\n")
        return 1

    print("  The walk works and finds what the index read misses. Safe to "
          "run live.\n")
    print("  Enemies in this dump, in list order (index 0 = most recently "
          "added):\n")
    for i, d in enumerate(ens):
        mark = "K" if d["keeper"] else " "
        vis = "" if d["ptr"] in seen_old else "   <- INVISIBLE to catchers()"
        print(f"  {mark}{row(i, d)[1:]}{vis}")
    print()
    return 0


# ---------------------------------------------------------------- live

def snapshot(mem, say):
    lid = None
    try:
        lid = mem.read_u32(0x003FF048)
    except Exception:
        pass
    act_n, dor_n = group_count(mem, ACTIVE_GROUP), group_count(mem,
                                                               DORMANT_GROUP)
    act, dor = walk(mem, ACTIVE_GROUP), walk(mem, DORMANT_GROUP)
    old = old_read(mem, ACTIVE_GROUP)

    say(f"  --- level {lid}   active {act_n} (walked {len(act)})   "
        f"dormant {dor_n} (walked {len(dor)}) ---")

    say("  ACTIVE  -- within 3000 units of Taz")
    if not act:
        say("    (empty)")
    for i, p in enumerate(act):
        d = describe(mem, p)
        mark = "K" if d["keeper"] else " "
        vis = "" if p in old else "   <<< catchers() CANNOT SEE THIS"
        say(f"  {mark}{row(i, d)[1:]}{vis}")

    missed = [p for p in act if p not in old]
    say(f"    old index read returned {len(old)} of {len(act)}"
        + (f", MISSING {len(missed)}" if missed else ""))

    beaten = []
    for p in dor:
        d = describe(mem, p)
        if d["keeper"] and d["alive"] == 0:
            beaten.append(d)
    if beaten:
        say("  DORMANT, permanently defeated (SUB+0x300 == 0):")
        for i, d in enumerate(beaten):
            say(row(i, d))
    say("")


def cmd_walk(args):
    mem = live_mem()
    if mem is None:
        return 1
    print("\n  Read-only. One snapshot.\n")
    snapshot(mem, print)
    return 0


def cmd_watch(args):
    """A TIMELINE, not a series of snapshots.

    The first version of this keyed its output on list MEMBERSHIP, so a
    keeper going idle -> defeated -> despawning with its E_ALIVE bit clearing
    underneath produced no output at all: the membership had not changed, so
    nothing printed. It was asked to show a transition it structurally could
    not show. This prints every field change as it happens.
    """
    import time
    mem = live_mem()
    if mem is None:
        return 1
    out = None
    if args.out:
        try:
            # PowerShell buffers a redirect and Ctrl-C throws the buffer
            # away, which lost a real capture once. Write it ourselves.
            out = open(args.out, "w", encoding="utf-8", buffering=1)
        except Exception as e:
            print(f"    could not open {args.out}: {e!r}")
            return 2

    def say(text=""):
        print(text)
        if out is not None:
            out.write(text + "\n")

    say("  Read-only. Walk up to a catcher and beat it.")
    if out is not None:
        say(f"  Writing to {args.out} as it happens -- Ctrl-C is safe.")
    say("  Ctrl-C to stop.")
    say("")
    say("  Every change is a line. What to look for on a kill:")
    say("    anim 2 -> 6      the takedown lands (state 6, defeated)")
    say("    alive 1 -> 0     the permanent bit clears at the 6 -> 14 handoff")
    say("    anim 6 -> 14     despawning")
    say("    GONE             and whether the totals say freed or reparented")
    say("")
    rc = watch_loop(mem, say)
    if out is not None:
        out.close()
    return rc


def watch_loop(mem, say, sleep=None, clock=None):
    """The timeline itself, with the clock and the wait injected.

    Separated so `selftest` can drive it over a scripted memory and PROVE it
    reports a kill, rather than the tool being handed to someone at an
    emulator on the strength of it looking right. That is exactly the mistake
    the first version made.
    """
    import time as _t
    sleep = _t.sleep if sleep is None else sleep
    clock = _t.time if clock is None else clock
    t0 = clock()

    def ev(text):
        say("  t+%7.2f  %s" % (clock() - t0, text))

    STATE = {2: "idle", 3: "suspicious", 4: "pursuing", 6: "DEFEATED",
             0xE: "despawning", 0: "dormant", 15: "idle15"}

    def label(d):
        return ("%s %08X" % ("keeper" if d["keeper"] else d["obe"][:8],
                             d["ptr"]))

    prev = {}          # ptr -> describe() dict
    prev_counts = None
    prev_lid = None
    try:
        while True:
            try:
                lid = mem.read_u32(0x003FF048)
            except Exception:
                lid = None
            act, dor = walk(mem, ACTIVE_GROUP), walk(mem, DORMANT_GROUP)
            an, dn = (group_count(mem, ACTIVE_GROUP),
                      group_count(mem, DORMANT_GROUP))
            # A short walk means a torn read, not a departure. Skip the whole
            # comparison rather than reporting enemies that never left.
            if an != len(act) or dn != len(dor):
                sleep(0.1)
                continue

            cur = {}
            for p, active in [(p, True) for p in act] + [(p, False)
                                                         for p in dor]:
                d = describe(mem, p)
                d["in_active"] = active
                cur[p] = d

            if lid != prev_lid:
                ev("LEVEL %s   active %s  dormant %s  total %s"
                   % (lid, an, dn, an + dn))
                prev_lid, prev, prev_counts = lid, {}, None

            counts = (an, dn, an + dn)
            if prev_counts is not None and counts != prev_counts:
                tag = ""
                if counts[2] < prev_counts[2]:
                    tag = "   <<< TOTAL DROPPED -- something was FREED"
                elif counts[2] > prev_counts[2]:
                    tag = "   (total rose -- something spawned)"
                else:
                    tag = "   (total unchanged -- a list-to-list move)"
                ev("counts active %d->%d  dormant %d->%d  total %d->%d%s"
                   % (prev_counts[0], counts[0], prev_counts[1], counts[1],
                      prev_counts[2], counts[2], tag))
            prev_counts = counts

            for p, d in cur.items():
                o = prev.get(p)
                if o is None:
                    ev("%-16s APPEARS in %s  anim=%s alive=%s  %s"
                       % (label(d), "active" if d["in_active"] else "dormant",
                          d["anim"], d["alive"], d["name"]))
                    continue
                if d["in_active"] != o["in_active"]:
                    ev("%-16s moved to %s" % (label(d), "ACTIVE"
                                              if d["in_active"] else "dormant"))
                if d["anim"] != o["anim"]:
                    ev("%-16s anim %s -> %s   %s"
                       % (label(d), o["anim"], d["anim"],
                          STATE.get(d["anim"], "?")))
                if d["alive"] != o["alive"]:
                    ev("%-16s **E_ALIVE %s -> %s**"
                       % (label(d), o["alive"], d["alive"]))
                if d["hit"] != o["hit"]:
                    ev("%-16s hit latch %s -> %s"
                       % (label(d), o["hit"], d["hit"]))

            for p, o in prev.items():
                if p not in cur:
                    ev("%-16s GONE from both lists   (last: anim=%s alive=%s "
                       "in %s)  %s"
                       % (label(o), o["anim"], o["alive"],
                          "active" if o["in_active"] else "dormant",
                          o["name"]))

            # The old-read comparison, only when it would actually differ.
            old = old_read(mem, ACTIVE_GROUP)
            missed = [p for p in act if p not in old]
            if missed and set(act) != set(x for x in prev if
                                          prev[x].get("in_active")):
                for p in missed:
                    ev("%-16s is INVISIBLE to the old index read (%d of %d)"
                       % (label(cur[p]), len(old), len(act)))

            prev = cur
            sleep(0.1)
    except KeyboardInterrupt:
        say("")
    say("  stopped")
    return 0


# ------------------------------------------------------- selftest (offline)

class ScriptMem:
    """A tiny fake EE with two real linked lists in it, driven by a script.

    Exists because the first version of `watch` was handed to someone at an
    emulator and could not observe the transition it was asked to show. This
    builds the structures for real -- heads, next/prev, counts, enemy objects
    with sub-objects -- and steps a keeper through idle -> defeated ->
    despawning -> freed, so `watch_loop` can be proven to report it before
    anyone spends time in front of PCSX2 again.
    """

    def __init__(self):
        self.w = {}                 # address -> u32
        self.b = {}                 # address -> bytes
        self.step_i = 0
        self.script = []
        for head in (ACTIVE_GROUP, DORMANT_GROUP):
            self._empty(head)

    # -- structure
    def _empty(self, head):
        self.w[head + L_NEXT] = head
        self.w[head + L_PREV] = head
        self.w[head + L_COUNT] = 0

    def add_enemy(self, ptr, sub, name, obe=b"keeper.obe"):
        self.b[ptr + E_TYPE] = obe + b"\0"
        self.b[ptr + E_NAME] = name.encode() + b"\0"
        self.w[ptr + E_SUB] = sub
        self.w[ptr + E_ATYPE] = 40
        for j in range(3):
            self.w[ptr + E_POS + j * 4] = 0
            self.w[sub + S_HOME + j * 4] = 0
        self.w[sub + S_ANIM] = 2
        self.w[sub + S_HIT] = 0
        self.w[sub + S_ALIVE] = 1
        self.w[sub + S_OUTER] = 0x45BB8000        # 3000.0
        self.w[sub + S_GRP_ACTIVE] = ACTIVE_GROUP
        self.w[sub + S_GRP_DORMANT] = DORMANT_GROUP

    def members(self, head):
        return walk(self, head)

    def put(self, head, ptrs):
        """Rebuild `head` to hold exactly `ptrs`, in order, counts correct."""
        if not ptrs:
            self._empty(head)
            return
        chain = [head] + list(ptrs) + [head]
        for i, node in enumerate(chain[:-1]):
            self.w[node + L_NEXT] = chain[i + 1]
        for i, node in enumerate(chain[1:], 1):
            self.w[node + L_PREV] = chain[i - 1]
        self.w[head + L_COUNT] = len(ptrs)
        for p in ptrs:
            self.w[p + L_OWNER] = head

    # -- the pcsx2_mem shape
    def read_u32(self, a):
        if a == 0x003FF048:
            return 5
        return self.w.get(a, 0)

    def read_float(self, a):
        import struct
        return struct.unpack("<f", struct.pack("<I", self.w.get(a, 0)))[0]

    def read_bytes(self, a, n):
        return (self.b.get(a, b"") + b"\0" * n)[:n]

    def valid_ptr(self, p):
        return p is not None and EE_MIN <= p < EE_MAX


def cmd_selftest(args):
    """Drive watch_loop over a scripted kill and assert it says so."""
    KEEP, KSUB = 0x00C97080, 0x00C9ADA0
    BEAR, BSUB = 0x00C7A5A0, 0x00C86A90
    m = ScriptMem()
    m.add_enemy(KEEP, KSUB, "enemy keeper05")
    m.add_enemy(BEAR, BSUB, "enemy brownbear01", obe=b"browbear.obe")

    def s0():                                   # both dormant
        m.put(DORMANT_GROUP, [BEAR, KEEP]); m.put(ACTIVE_GROUP, [])

    def s1():                                   # Taz approaches: both active
        m.put(ACTIVE_GROUP, [KEEP, BEAR]); m.put(DORMANT_GROUP, [])

    def s2():                                   # the takedown lands
        m.w[KSUB + S_HIT] = 1
        m.w[KSUB + S_ANIM] = 6

    def s3():                                   # the 6 -> 14 handoff
        m.w[KSUB + S_ANIM] = 0xE
        m.w[KSUB + S_ALIVE] = 0

    def s4():                                   # freed: out of both lists
        m.put(ACTIVE_GROUP, [BEAR]); m.put(DORMANT_GROUP, [])

    m.script = [s0, s0, s1, s1, s2, s2, s3, s3, s4, s4]

    lines = []
    tick = [0]

    def sleep(_):
        if tick[0] >= len(m.script):
            raise KeyboardInterrupt
        m.script[tick[0]]()
        tick[0] += 1

    def clock():
        return tick[0] * 0.1

    m.script[0]()
    watch_loop(m, lines.append, sleep=sleep, clock=clock)

    print()
    for ln in lines:
        print(ln)
    print()

    text = "\n".join(lines)
    want = [
        ("the keeper is seen at all", "00C97080  APPEARS"),
        ("the takedown -- anim to state 6", "anim 2 -> 6"),
        ("...named as DEFEATED", "DEFEATED"),
        ("**the E_ALIVE transition**", "**E_ALIVE 1 -> 0**"),
        ("the despawn state", "anim 6 -> 14"),
        ("the keeper leaving both lists", "GONE from both lists"),
        ("and the total dropping, i.e. FREED not reparented",
         "TOTAL DROPPED"),
        ("a list-to-list move is NOT reported as a drop",
         "total unchanged -- a list-to-list move"),
    ]
    bad = 0
    for label, needle in want:
        ok = needle in text
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    print()
    if bad:
        print(f"  {bad} of {len(want)} FAILED -- this tool cannot show what "
              f"it claims to. Do NOT run it live.\n")
        return 1
    print(f"  {len(want)}/{len(want)}. The timeline reports a kill, the "
          f"E_ALIVE transition and the free.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    ck = sub.add_parser("check", help="offline, against ee_dump.bin")
    ck.add_argument("--dump", default="", help="path to the dump")
    ck.set_defaults(fn=cmd_check)
    sub.add_parser("walk", help="live, one snapshot").set_defaults(
        fn=cmd_walk)
    sub.add_parser("selftest",
                   help="offline, prove watch can see a kill").set_defaults(
        fn=cmd_selftest)
    w = sub.add_parser("watch", help="live, continuous")
    w.add_argument("--out", default="", help="also write to this file")
    w.set_defaults(fn=cmd_watch)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
