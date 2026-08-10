#!/usr/bin/env python3
"""Stress the stolen-subtitle mechanism the way an AP session actually will.

Three things have to hold up:

  rate      items can arrive in bursts. There is exactly ONE stolen object,
            so ours are strictly serial -- the queue lives here, and this
            measures whether it drains and what it costs.
  patience  the game has two slots. If a real message owns ours, the tick
            will not open a page for our object until it frees. We wait; we
            never take a slot off the game.
  coexist   a real message can arrive while ours is up, sit on list A behind
            ours, and tear itself down normally. We must unlink only OUR
            node, never force a slot holding a page we did not open, and
            never leave a foreign page redirected at our stub.

Every scenario re-checks the same invariants after every message:

  * both stolen blocks still validate (key+base, in-use halfword, contiguity)
  * list A's count matches its chain, and the chain has no cycle
  * every foreign node on list A is a valid block we have not touched
  * every foreign page still has +0x1D8 == end_message
  * both slots hold either nothing or a valid block
  * heap growth per message stays under the budget

    py -3.13 taz_stress.py burst --n 20
    py -3.13 taz_stress.py flood --n 30 --gap 0.2
    py -3.13 taz_stress.py soak --minutes 10
    py -3.13 taz_stress.py coexist --minutes 5

`soak` and `coexist` are the ones to run while actually playing -- go collect
sandwiches and walk into hint triggers so real messages fight ours.
"""

import argparse
import collections
import sys
import time

import taz_steal as T

DEFAULT_IDS = [1339, 1394, 1465, 422, 1472]
BUDGET = 0x400          # bytes of heap growth per message before we complain


class Harness:
    def __init__(self, p, st, ids, mode, flags, base_seconds, floor_seconds,
                 budget):
        self.p, self.st, self.mode, self.flags = p, st, mode, flags
        self.ids = ids
        self.base_seconds, self.floor_seconds = base_seconds, floor_seconds
        self.budget = budget
        self.queue = collections.deque()
        self.sent = self.failed = 0
        self.foreign_seen = {}
        self.foreign_done = 0
        self.problems = []
        self.heap0 = p.r32(T.IN_USE_TOTAL)
        self.our_pages = set()

    # ---------------------------------------------------------- invariants

    def note(self, msg):
        self.problems.append(msg)
        print(f"      !! {msg}")

    def check(self, where):
        p, st = self.p, self.st
        found = []

        ok, why = T.validate(p, st["node"], st.get("node_size"))
        if not ok:
            found.append(f"{where}: stolen node invalid ({why})")
        ok, why = T.validate(p, st["obj"], st.get("obj_size"))
        if not ok:
            found.append(f"{where}: stolen object invalid ({why})")

        nodes, count = T.list_walk(p)
        if any(n[2] == "CYCLE" for n in nodes):
            found.append(f"{where}: list A chain has a cycle")
        elif count != len(nodes):
            found.append(f"{where}: list A count {count} but chain {len(nodes)}")

        for n, v, _ in nodes:
            if n == st["node"]:
                continue
            ok, why = T.validate(p, n)
            if not ok:
                found.append(f"{where}: foreign node 0x{n:08X} invalid ({why})")
            if v and T.ee(v):
                page = p.r32(v + T.O_PAGE)
                if T.ee(page) and page not in self.our_pages:
                    cb = p.r32(page + T.PAGE_CB)
                    if cb != T.END_MESSAGE:
                        found.append(
                            f"{where}: foreign page 0x{page:08X} callback is "
                            f"0x{cb:08X}, not end_message -- we touched it")
                self.foreign_seen.setdefault(n, {
                    "obj": v, "id": p.r32(v + T.O_INDEX), "t": time.time()})

        for k in ("A", "B"):
            s = T.slot_state(p, k)
            if s["open"]:
                ok, why = T.validate(p, s["open"])
                if not ok:
                    found.append(f"{where}: slot {k} holds 0x{s['open']:08X} "
                                 f"which is not a live block ({why})")

        live = {n for n, _, _ in nodes}
        for n in list(self.foreign_seen):
            if n not in live:
                self.foreign_seen.pop(n)
                self.foreign_done += 1

        for f in found:
            self.note(f)
        return not found

    # -------------------------------------------------------------- queue

    def submit(self, index):
        self.queue.append(index)

    def duration(self):
        """Backpressure: the deeper the queue, the shorter each message."""
        q = len(self.queue)
        if q <= 1:
            return self.base_seconds
        return max(self.floor_seconds, self.base_seconds / (1 + 0.5 * (q - 1)))

    def pump(self):
        """Show one queued message. Returns a result dict or None if idle."""
        if not self.queue:
            return None
        index = self.queue.popleft()
        secs = self.duration()
        r = T.one_run(self.p, self.st, index, secs, self.flags, self.mode,
                      slot_wait=30.0, log=lambda s: None)
        if r.get("error"):
            self.failed += 1
            print(f"      id {index}: {r['error']}")
            return r
        self.our_pages.add(r["page"])
        self.sent += 1
        flag = "ok " if r["ok"] else "LOST"
        print(f"      id {index:5d}  {secs:4.1f}s  q{len(self.queue):<3d} "
              f"page 0x{r['page']:08X}  cost {r['cost']:+#7x}  "
              f"slot {'self' if r['freed_itself'] else 'FORCED'}  {flag}"
              + ("  (queued behind a real message)" if r["queued"] else ""))
        if not r["ok"]:
            self.note(f"blocks lost after id {index}: {r['why']}")
        if r["cost"] > self.budget:
            self.note(f"id {index} cost {r['cost']:#x}, over budget "
                      f"{self.budget:#x}")
        if not r["freed_itself"] and self.mode == "stub":
            self.note(f"id {index}: slot had to be forced -- close_page did "
                      "not run, so the stub is not doing its job")
        self.check(f"after id {index}")
        return r

    # ------------------------------------------------------------- report

    def report(self, seconds):
        p = self.p
        grew = (p.r32(T.IN_USE_TOTAL) - self.heap0) & 0xFFFFFFFF
        print()
        print(f"    shown {self.sent}, failed {self.failed}, "
              f"still queued {len(self.queue)}")
        print(f"    real game messages seen come and go: {self.foreign_done}"
              + (f", {len(self.foreign_seen)} still up" if self.foreign_seen else ""))
        print(f"    heap grew {grew:#x} bytes over {seconds:.0f}s"
              + (f" ({grew // self.sent} per message)" if self.sent else ""))
        ok, why = T.validate(p, self.st["node"], self.st.get("node_size"))
        print(f"    stolen node   {'ok' if ok else why}")
        ok2, why2 = T.validate(p, self.st["obj"], self.st.get("obj_size"))
        print(f"    stolen object {'ok' if ok2 else why2}")
        nodes, count = T.list_walk(p)
        print(f"    list A count {count}, chain {len(nodes)}")
        for k in ("A", "B"):
            s = T.slot_state(p, k)
            print(f"    slot {k} {'busy 0x%08X' % s['open'] if s['open'] else 'free'}")
        print()
        if self.problems:
            print(f"    {len(self.problems)} PROBLEM(S):")
            for m in self.problems[:20]:
                print(f"      {m}")
            if len(self.problems) > 20:
                print(f"      ... and {len(self.problems) - 20} more")
            return 1
        print("    No invariant broke.")
        return 0


def setup(args):
    p = T.Pine().connect()
    st = T.stolen(p)
    if not st:
        raise SystemExit(1)
    if args.mode == "stub":
        for k in ("A", "B"):
            if not T.stub_ok(p, k):
                raise SystemExit(
                    f"    the slot {k} stub is not installed.\n"
                    "    Run: taz_steal.py stub     (savestate first)\n"
                    "    Or stress the leaky path with --mode disarm.")
    ids = [i for i in (args.ids or DEFAULT_IDS) if T.entry_text(p, i)]
    if not ids:
        raise SystemExit("    none of those string ids resolve to text")
    T.detach(p, st["node"])
    h = Harness(p, st, ids, args.mode, args.flags,
                args.seconds, args.floor, args.budget)
    print(f"    node 0x{st['node']:08X}  object 0x{st['obj']:08X}  "
          f"mode {args.mode}  ids {ids}")
    print(f"    heap at start 0x{p.r32(T.IN_USE_TOTAL):08X}")
    print()
    return h


def cmd_burst(args):
    """Everything arrives at once, as a big item batch would."""
    h = setup(args)
    t0 = time.time()
    for i in range(args.n):
        h.submit(h.ids[i % len(h.ids)])
    print(f"    {args.n} messages queued instantly. Draining...")
    while h.queue:
        if h.pump() is None:
            break
    return h.report(time.time() - t0)


def cmd_flood(args):
    """They keep arriving while the queue is still draining."""
    h = setup(args)
    t0, produced, next_at = time.time(), 0, time.time()
    while produced < args.n or h.queue:
        now = time.time()
        while produced < args.n and now >= next_at:
            h.submit(h.ids[produced % len(h.ids)])
            produced += 1
            next_at += args.gap
        if h.queue:
            if h.pump() is None:
                break
        else:
            time.sleep(0.05)
    print(f"\n    produced {produced} at one every {args.gap}s")
    return h.report(time.time() - t0)


def cmd_soak(args):
    """Steady drip for a long time. Play while this runs."""
    h = setup(args)
    print("    Play normally. Collect sandwiches, walk into hint triggers --")
    print("    real messages fighting ours is the point of this one.")
    print()
    t0, end, i, next_at = time.time(), time.time() + args.minutes * 60, 0, 0.0
    while time.time() < end:
        if time.time() >= next_at:
            h.submit(h.ids[i % len(h.ids)])
            i += 1
            next_at = time.time() + args.gap
        if h.queue:
            if h.pump() is None:
                break
        else:
            h.check("idle")
            time.sleep(0.25)
    return h.report(time.time() - t0)


def cmd_coexist(args):
    """Send ours only when a REAL message is already up, to force contention."""
    h = setup(args)
    print("    Waiting for real messages. Go trigger some -- each time one")
    print("    appears, one of ours is queued behind it on purpose.")
    print()
    t0, end, i, armed = time.time(), time.time() + args.minutes * 60, 0, True
    while time.time() < end:
        nodes, _ = T.list_walk(h.p)
        foreign = [n for n, _, _ in nodes if n != h.st["node"]]
        if foreign and armed:
            print(f"      real message on list A ({len(foreign)} node(s)) "
                  "-> queueing ours behind it")
            h.submit(h.ids[i % len(h.ids)])
            i += 1
            armed = False
        elif not foreign:
            armed = True
        if h.queue:
            if h.pump() is None:
                break
        else:
            h.check("idle")
            time.sleep(0.2)
    return h.report(time.time() - t0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("stub", "disarm"), default="stub")
    ap.add_argument("--flags", type=lambda x: int(x, 0), default=2,
                    help="2 = slot A, 0 = slot B")
    ap.add_argument("--seconds", type=float, default=2.5,
                    help="display time with an empty queue")
    ap.add_argument("--floor", type=float, default=0.8,
                    help="shortest display time under backpressure")
    ap.add_argument("--budget", type=lambda x: int(x, 0), default=BUDGET,
                    help="heap growth per message before complaining")
    ap.add_argument("--ids", type=int, nargs="*")
    sub = ap.add_subparsers(dest="verb", required=True)

    b = sub.add_parser("burst")
    b.add_argument("--n", type=int, default=20)
    b.set_defaults(fn=cmd_burst)

    f = sub.add_parser("flood")
    f.add_argument("--n", type=int, default=30)
    f.add_argument("--gap", type=float, default=0.2)
    f.set_defaults(fn=cmd_flood)

    s = sub.add_parser("soak")
    s.add_argument("--minutes", type=float, default=10.0)
    s.add_argument("--gap", type=float, default=6.0)
    s.set_defaults(fn=cmd_soak)

    c = sub.add_parser("coexist")
    c.add_argument("--minutes", type=float, default=5.0)
    c.set_defaults(fn=cmd_coexist)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n    interrupted -- run `taz_steal.py audit` to see the state")
        return 130


if __name__ == "__main__":
    sys.exit(main())
