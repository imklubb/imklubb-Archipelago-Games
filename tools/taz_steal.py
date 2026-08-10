#!/usr/bin/env python3
"""Repeatable subtitles for Taz: Wanted, using the game's own machinery.

The mechanism, read out of the EE dump rather than guessed:

  raise_subtitle 0x002C56E8   obj = alloc(0x10); {page, index, duration, flags}
  tick           0x002C5838   per frame, per object on list A:
                                slot = (obj[0x0C] & 2) ? A : B
                                if obj[0x00] and obj[0x08] > 0:
                                    obj[0x08] -= dt
                                    if <= 0: obj[0x08] = 0; 0x0013BEA0(slot, 0)
                                if slot[0x194]: skip
                                0x002C59E8(obj, slot)   opens the page
  page closed    0x00138B30   cb = page[0x1D8]; if cb: cb(page, page[0x1DC])
  end_message    0x002C5650   if (arg == 0) return
                              0x0013CDA0(slot, page, 1)   full page teardown
                              remove_first(list A, arg)   erase() frees the node
                              free(arg)                   frees the object

Two ways to keep our stolen node and object alive when a page closes:

  disarm   page[0x1DC] = 0     end_message returns immediately. Nothing of
                               ours is freed -- but 0x0013CDA0 never runs
                               either, so the page, its 0x3B0 sibling and
                               everything they own leak, and slot[0x194]
                               has to be cleared by hand. ~0xB10 a message.

  stub     page[0x1D8] = our   five instructions in scratch that tail-call
                               0x0013CDA0(slot, page, 1) and return. The
                               page is destroyed properly and the slot
                               releases itself. remove_first and free are
                               never reached. No leak.

Header validation is exact, not heuristic: every block's word at +0x00 is its
offset from the heap base at [0x00512CA4], the in-use halfword at +0x08 is
only ever 0 or 1, and next_phys is always at + size. Checked against all
6810 blocks in a live dump.

    check      are the stolen blocks still ours
    audit      list A, both slots, heap, scratch, stub
    watch      decode whatever subtitle is up, write nothing
    steal      take a real node + object from a real message
    stub       install / verify / remove the teardown stub
    run <id>   one message, every step reported
    cycle <n>  n of them, measuring cost per message
    drop       detach our node from list A (leaves foreign nodes alone)
"""

import argparse
import json
import os
import socket
import struct
import sys
import time

SLOT_PINE = 28011
READ32, WRITE32 = 2, 6

LIST_A = 0x00508FE0
L_CURSOR, L_HEAD, L_ITER, L_INDEX, L_COUNT = 0x20, 0x24, 0x28, 0x2C, 0x30
N_VALUE, N_PREV, N_NEXT = 0x20, 0x24, 0x30
O_PAGE, O_INDEX, O_DURATION, O_FLAGS = 0x00, 0x04, 0x08, 0x0C

PAGE_CB, PAGE_ARG = 0x1D8, 0x1DC
END_MESSAGE = 0x002C5650
CLOSE_PAGE = 0x0013CDA0

SLOTS = {"A": 0x004746A0, "B": 0x00474490}
SLOT_PAGES, SLOT_OPEN = 0x138, 0x194
TICK_OPEN = {"A": 0x004748B8, "B": 0x004748B4}

STR_TABLE = 0x0069D250
HDR = 0x20
H_KEY, H_SIZE, H_FLAGS, H_TAG = 0x00, 0x04, 0x08, 0x0C
H_NEXT, H_PREV, H_NEXTP, H_PREVP = 0x10, 0x14, 0x18, 0x1C
IN_USE_TOTAL = 0x00512C9C
HEAP_BASE_PTR = 0x00512CA4

STUB_DEFAULT = 0x01F00800          # clear of the probe's 0x01F00000 scratch
EE_MIN, EE_MAX = 0x00100000, 0x02000000
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "taz_steal.json")


class Pine:
    def __init__(self, slot=SLOT_PINE):
        self.slot, self.sock = slot, None

    def connect(self):
        if sys.platform == "win32":
            family, name = socket.AF_INET, ("127.0.0.1", self.slot)
        elif sys.platform == "darwin":
            family = socket.AF_UNIX
            name = os.environ.get("TMPDIR", "/tmp") + "/pcsx2.sock"
        else:
            family = socket.AF_UNIX
            name = os.environ.get("XDG_RUNTIME_DIR", "/tmp") + "/pcsx2.sock"
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(10.0)
        try:
            s.connect(name)
        except socket.error as e:
            s.close()
            raise SystemExit(f"    could not reach PCSX2 on {name!r}: {e}\n"
                             "    Is the AP client still open?")
        self.sock = s
        return self

    def _send(self, req):
        self.sock.sendall(req)
        want, buf = 4, b""
        while len(buf) < want:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("PCSX2 closed the connection.")
            buf += chunk
            if want == 4 and len(buf) >= 4:
                want = int.from_bytes(buf[0:4], "little")
        if buf[4] == 0xFF:
            raise ConnectionError("PCSX2 reported a failure")
        return buf

    def _one(self, cmd, addr, extra=b""):
        body = bytes([cmd]) + addr.to_bytes(4, "little") + extra
        return self._send((len(body) + 4).to_bytes(4, "little") + body)

    def r32(self, a):
        return int.from_bytes(self._one(READ32, a)[5:9], "little")

    def w32(self, a, v):
        self._one(WRITE32, a, (v & 0xFFFFFFFF).to_bytes(4, "little"))

    def many(self, addrs):
        if not addrs:
            return []
        body = b"".join(bytes([READ32]) + a.to_bytes(4, "little") for a in addrs)
        r = self._send((len(body) + 4).to_bytes(4, "little") + body)
        return [int.from_bytes(r[5 + 4 * i:9 + 4 * i], "little")
                for i in range(len(addrs))]

    def f32(self, a):
        return struct.unpack("<f", self.r32(a).to_bytes(4, "little"))[0]

    def wf32(self, a, v):
        self.w32(a, struct.unpack("<I", struct.pack("<f", v))[0])

    def bytes_at(self, a, n):
        return b"".join(v.to_bytes(4, "little")
                        for v in self.many(list(range(a, a + n, 4))))


def ee(v):
    return EE_MIN <= v < EE_MAX


def entry_text(p, index):
    if not 0 <= index < 4000:
        return None
    e = STR_TABLE + index * 0x10
    ptr, length = p.r32(e), p.r32(e + 4)
    if not ee(ptr) or not 0 < length < 400:
        return None
    return p.bytes_at(ptr, ((length * 2) + 3) & ~3)[:length * 2] \
            .decode("utf-16le", "replace")


# ------------------------------------------------------- block validation

def hdr(p, blk):
    b = blk - HDR
    w = p.many([b + o for o in (H_KEY, H_SIZE, H_FLAGS, H_TAG,
                                H_NEXT, H_PREV, H_NEXTP, H_PREVP)])
    h = dict(zip(("key", "size", "flags", "tag",
                  "next", "prev", "next_phys", "prev_phys"), w))
    h["at"], h["block"] = b, blk
    return h


def validate(p, blk, want_size=None):
    """Exact, not heuristic. Returns (ok, why).

    key + heap_base == header address held for all 6810 blocks in a dump,
    and the in-use halfword is only ever 0 or 1, so a reissued or overwritten
    block cannot pass this by accident.
    """
    if not ee(blk) or blk & 0xF:
        return False, "not a 16-byte-aligned EE address"
    h = hdr(p, blk)
    base = p.r32(HEAP_BASE_PTR)
    if not ee(base):
        return False, "heap base is not sane"
    if (h["key"] + base) & 0xFFFFFFFF != h["at"]:
        return False, (f"key 0x{h['key']:08X} + base 0x{base:08X} "
                       f"!= header 0x{h['at']:08X}")
    low = h["flags"] & 0xFFFF
    if low != 1:
        return False, ("marked free" if low == 0
                       else f"in-use halfword is 0x{low:04X}, not 0 or 1")
    if h["size"] & 0xF or not (0x20 <= h["size"] < 0x100000):
        return False, f"size 0x{h['size']:X} is not a sane block size"
    if h["next_phys"] != h["at"] + h["size"]:
        return False, (f"next_phys 0x{h['next_phys']:08X} != "
                       f"0x{h['at'] + h['size']:08X}")
    if want_size is not None and h["size"] != want_size:
        return False, f"size changed 0x{want_size:X} -> 0x{h['size']:X}"
    return True, "ok"


def show_block(p, name, blk, want_size=None):
    ok, why = validate(p, blk, want_size)
    h = hdr(p, blk)
    print(f"      {name:7s} 0x{blk:08X}  size 0x{h['size']:<6X} "
          f"flags 0x{h['flags']:08X}  {'OK' if ok else 'BAD: ' + why}")
    return ok


# ------------------------------------------------------------- list A

def list_walk(p, limit=64):
    """Every node on list A, in chain order, plus the declared count."""
    count = p.r32(LIST_A + L_COUNT)
    out, seen, n = [], set(), p.r32(LIST_A + L_CURSOR)
    while ee(n) and n != LIST_A and len(out) < limit:
        if n in seen:
            out.append((n, None, "CYCLE"))
            break
        seen.add(n)
        out.append((n, p.r32(n + N_VALUE), None))
        n = p.r32(n + N_NEXT)
    return out, count


def attach(p, node, obj):
    """Push our node on the front of list A, leaving any others intact."""
    first = p.r32(LIST_A + L_CURSOR)
    p.w32(node + N_VALUE, obj)
    p.w32(node + N_PREV, LIST_A)
    p.w32(node + N_NEXT, first)
    p.w32(first + N_PREV, node)
    p.w32(LIST_A + L_CURSOR, node)
    p.w32(LIST_A + L_COUNT + 4, 0)
    p.w32(LIST_A + L_COUNT, p.r32(LIST_A + L_COUNT) + 1)


def detach(p, node):
    """Unlink one node, exactly the way erase() does, minus the free().

    The old code emptied the whole list, which would have thrown away a real
    game message that happened to be queued behind ours -- leaking its node
    and object and losing the message.
    """
    nodes, _ = list_walk(p)
    if node not in [n for n, _, _ in nodes]:
        return False
    cursor = p.r32(LIST_A + L_CURSOR)
    nxt, prv = p.r32(node + N_NEXT), p.r32(node + N_PREV)
    if node == cursor:
        p.w32(LIST_A + L_CURSOR, nxt)
        p.w32(nxt + N_PREV, LIST_A)
    else:
        p.w32(prv + N_NEXT, nxt)
    if node == p.r32(LIST_A + L_ITER):
        p.w32(LIST_A + L_ITER, 0)
        p.w32(LIST_A + L_INDEX, 0xFFFFFFFE)
    p.w32(nxt + N_PREV, prv)
    p.w32(LIST_A + L_COUNT + 4, 0)
    p.w32(LIST_A + L_COUNT, max(p.r32(LIST_A + L_COUNT) - 1, 0))
    return True


def empty_list(p):
    """Sledgehammer. Only for a stuck screen -- it abandons foreign nodes."""
    for off, val in ((L_CURSOR, LIST_A), (L_HEAD, LIST_A), (L_ITER, 0),
                     (L_INDEX, 0xFFFFFFFE), (L_COUNT + 4, 0), (L_COUNT, 0)):
        p.w32(LIST_A + off, val)


# -------------------------------------------------------------- slots

def slot_state(p, key):
    base = SLOTS[key]
    c = base + SLOT_PAGES
    return {"key": key, "base": base, "pages": c,
            "open": p.r32(base + SLOT_OPEN),
            "count": p.r32(c + L_COUNT), "tick": p.r32(TICK_OPEN[key])}


def unjam(p, key, only_page=None, quiet=False):
    """Force a slot back to idle. Refuses unless the open page is ours.

    Clearing a slot that a real game message owns would strand its page and
    lose the message, so only_page must match.
    """
    s = slot_state(p, key)
    if not s["open"]:
        return False
    if only_page is not None and s["open"] != only_page:
        if not quiet:
            print(f"    slot {key} holds page 0x{s['open']:08X}, not ours "
                  f"(0x{only_page:08X}). Leaving it alone.")
        return False
    c = s["pages"]
    for off, val in ((L_CURSOR, c), (L_HEAD, c), (L_ITER, 0),
                     (L_INDEX, 0xFFFFFFFE), (L_COUNT + 4, 0), (L_COUNT, 0)):
        p.w32(c + off, val)
    p.w32(s["base"] + SLOT_OPEN, 0)
    p.w32(TICK_OPEN[key], 0)
    if not quiet:
        print(f"    slot {key}: force-released page 0x{s['open']:08X} (leaked)")
    return True


# --------------------------------------------------------------- stub

def stub_words(slot_addr):
    return [0x0080282D,                                   # move  a1, a0
            0x3C040000 | ((slot_addr >> 16) & 0xFFFF),    # lui   a0, hi
            0x34840000 | (slot_addr & 0xFFFF),            # ori   a0, a0, lo
            0x08000000 | (CLOSE_PAGE >> 2),               # j     close_page
            0x24060001]                                   # addiu a2, zero, 1


def scratch_idle(p, addr, span):
    if not ee(addr) or addr & 0xF or not ee(addr + span):
        return False
    if any(p.bytes_at(addr, span)):
        return False
    time.sleep(0.3)
    return not any(p.bytes_at(addr, span))


def stub_addrs(base=None):
    b = base if base is not None else load().get("stub_base", STUB_DEFAULT)
    return {"A": b, "B": b + 0x20}


def stub_ok(p, key, base=None):
    a = stub_addrs(base)[key]
    want = stub_words(SLOTS[key])
    return p.many([a + 4 * i for i in range(5)]) == want


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(st):
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=2)


def stolen(p, quiet=False):
    st = load().get("stolen")
    if not st:
        if not quiet:
            print("    Nothing stolen yet. Run `steal` first.")
        return None
    for name, size in (("node", "node_size"), ("obj", "obj_size")):
        ok, why = validate(p, st[name], st.get(size))
        if not ok:
            if not quiet:
                print(f"    stolen {name} 0x{st[name]:08X} is no longer ours: "
                      f"{why}")
                print("    Nothing will be written to it. Steal a fresh pair.")
            return None
    return st


# --------------------------------------------------------------- run

def one_run(p, st, index, seconds, flags, mode="stub",
            slot_wait=20.0, log=print):
    """One message start to finish. Returns a result dict, or None.

    The object comes off list A the instant its timer reaches zero. Leaving
    it on is what cost the blocks the first time: once the slot frees, the
    tick opens a SECOND page for the same object, armed, and its teardown
    frees the node and the object.
    """
    node, obj = st["node"], st["obj"]
    key = "A" if flags & 2 else "B"
    if entry_text(p, index) is None:
        return {"error": f"string {index} is not text"}
    if mode == "stub" and not stub_ok(p, key):
        return {"error": f"stub for slot {key} is not installed or intact"}

    t_start = time.time()
    before = p.r32(IN_USE_TOTAL)
    p.w32(obj + O_PAGE, 0)
    p.w32(obj + O_INDEX, index)
    p.wf32(obj + O_DURATION, seconds)
    p.w32(obj + O_FLAGS, flags)
    attach(p, node, obj)

    # Wait for the tick to build a page. It will not while a real game
    # message owns the slot, so this can legitimately take a while.
    page, waited, end = 0, False, time.time() + slot_wait
    while time.time() < end:
        cand = p.r32(obj + O_PAGE)
        if ee(cand) and p.r32(cand + PAGE_CB) == END_MESSAGE:
            page = cand
            break
        if slot_state(p, key)["open"]:
            waited = True
        time.sleep(0.004)
    if not page:
        detach(p, node)
        return {"error": f"no page within {slot_wait}s (slot {key} busy)"}
    opened = time.time()

    if mode == "stub":
        p.w32(page + PAGE_CB, stub_addrs()[key])
        if p.r32(page + PAGE_CB) != stub_addrs()[key]:
            p.w32(page + PAGE_CB, END_MESSAGE)
            detach(p, node)
            return {"error": "could not redirect page+0x1D8"}
    else:
        p.w32(page + PAGE_ARG, 0)
        if p.r32(page + PAGE_ARG) != 0:
            detach(p, node)
            return {"error": "could not disarm page+0x1DC"}

    # Timer. Detach the moment it hits zero, before the slot can free up.
    end = time.time() + seconds + 5.0
    while time.time() < end and p.f32(obj + O_DURATION) > 0.0:
        time.sleep(0.006)
    detach(p, node)
    p.w32(obj + O_PAGE, 0)

    # With the stub, close_page runs and the slot releases itself. That is
    # the whole proof, so wait for it rather than forcing it.
    freed_itself, end = False, time.time() + 4.0
    while time.time() < end:
        if not slot_state(p, key)["open"]:
            freed_itself = True
            break
        time.sleep(0.02)
    forced = False
    if not freed_itself:
        forced = unjam(p, key, only_page=page, quiet=True)

    time.sleep(0.15)
    after = p.r32(IN_USE_TOTAL)
    n_ok, n_why = validate(p, node, st.get("node_size"))
    o_ok, o_why = validate(p, obj, st.get("obj_size"))
    return {"ok": n_ok and o_ok, "node_ok": n_ok, "obj_ok": o_ok,
            "why": None if (n_ok and o_ok) else (n_why if not n_ok else o_why),
            "cost": (after - before) & 0xFFFFFFFF,
            "page": page, "freed_itself": freed_itself, "forced": forced,
            "queued": waited, "slot": key,
            "open_delay": opened - t_start, "total": time.time() - t_start}


# -------------------------------------------------------------- verbs

def cmd_check(p, args):
    st = load().get("stolen")
    if not st:
        print("    Nothing stolen yet.")
        return 1
    print(f"    stolen {st['when']}")
    a = show_block(p, "node", st["node"], st.get("node_size"))
    b = show_block(p, "object", st["obj"], st.get("obj_size"))
    if a and b:
        print(f"\n      page 0x{p.r32(st['obj'] + O_PAGE):08X}   "
              f"index {p.r32(st['obj'] + O_INDEX)}   "
              f"timer {p.f32(st['obj'] + O_DURATION):.3f}   "
              f"flags 0x{p.r32(st['obj'] + O_FLAGS):08X}")
        print("\n    Both blocks are still ours.")
        return 0
    print("\n    LOST. Get a real message up and `steal` again.")
    return 1


def cmd_audit(p, args):
    st = load().get("stolen")
    print(f"    heap base 0x{p.r32(HEAP_BASE_PTR):08X}   "
          f"in-use total 0x{p.r32(IN_USE_TOTAL):08X}")
    print()
    nodes, count = list_walk(p)
    print(f"    list A: count says {count}, chain has {len(nodes)}"
          + ("   MISMATCH" if count != len(nodes) else ""))
    for n, v, note in nodes:
        mine = st and n == st["node"]
        ok, why = validate(p, n)
        idx = p.r32(v + O_INDEX) if v and ee(v) else -1
        txt = entry_text(p, idx) if idx >= 0 else None
        print(f"      node 0x{n:08X} -> obj 0x{(v or 0):08X}  "
              f"{'OURS  ' if mine else 'foreign'}  "
              f"{'ok' if ok else why}  id {idx}  {txt!r}")
        if note:
            print(f"        {note}")
    print()
    for k in ("A", "B"):
        s = slot_state(p, k)
        print(f"    slot {k} 0x{s['base']:08X}  "
              f"{'BUSY page 0x%08X' % s['open'] if s['open'] else 'free'}   "
              f"pages count {s['count']}   tick {s['tick']}   "
              f"stub {'installed' if stub_ok(p, k) else 'absent'}")
    print()
    if st:
        show_block(p, "node", st["node"], st.get("node_size"))
        show_block(p, "object", st["obj"], st.get("obj_size"))
    return 0


def cmd_watch(p, args):
    print(f"    waiting up to {args.wait}s for a subtitle...")
    end = time.time() + args.wait
    while time.time() < end:
        nodes, _ = list_walk(p)
        if nodes:
            for n, o, _ in nodes:
                if not (o and ee(o)):
                    continue
                page = p.r32(o + O_PAGE)
                idx, flags = p.r32(o + O_INDEX), p.r32(o + O_FLAGS)
                print(f"\n    node 0x{n:08X}  object 0x{o:08X}  "
                      f"page 0x{page:08X}")
                print(f"    index {idx}  timer {p.f32(o + O_DURATION):.3f}  "
                      f"flags 0x{flags:08X} -> slot {'A' if flags & 2 else 'B'}")
                print(f"    text {entry_text(p, idx)!r}")
                if ee(page):
                    print(f"    page +0x1D8 0x{p.r32(page + PAGE_CB):08X}  "
                          f"+0x1DC 0x{p.r32(page + PAGE_ARG):08X}")
                show_block(p, "node", n)
                show_block(p, "object", o)
            print("\n    Nothing was written.")
            return 0
        time.sleep(0.01)
    print("    None appeared. Try:  probe sandwiches --set 99, collect one.")
    return 1


LEVEL_ID = 0x003FF048
SAVE_BASE, SAVE_STRIDE, FIRST_LEVEL, FILE_STRIDE = 0x00400444, 0x238, 3, 0x42B4
LIVE_OFFSET, SANDWICHES = 0x8568, 0x1E4


def bait(p, save_file=0):
    """Set the current level's sandwich count to 99, both copies.

    The hundred-sandwich message is the most reliable real message to catch,
    and the count has to be right in the live array as well as the saved one
    -- the HUD and the trigger read the live copy.
    """
    level = p.r32(LEVEL_ID)
    if not FIRST_LEVEL <= level <= 20:
        print(f"    level id reads {level}, which is not a level. Load one.")
        return False
    saved = (SAVE_BASE + (level - FIRST_LEVEL) * SAVE_STRIDE
             + save_file * FILE_STRIDE + SANDWICHES)
    live = saved + LIVE_OFFSET
    was = (p.r32(saved), p.r32(live))
    p.w32(saved, 99)
    p.w32(live, 99)
    print(f"    level {level}: sandwiches 0x{saved:08X} {was[0]} -> 99, "
          f"0x{live:08X} {was[1]} -> 99")
    print("    Collect one more sandwich to fire the message.")
    return True


LEVELS = {3: "Yosemite Zoo (hub)", 4: "Ice Burg", 5: "Zooney Tunes",
          6: "Looney Lagoon", 7: "Elephant Pong", 8: "Sam Francisco (hub)",
          9: "Looningdale's", 10: "Samsonian Museum", 11: "Bank of Samerica",
          12: "Gladiatoons", 13: "Wile E. West (hub)", 14: "Taz: Haunted",
          15: "Cartoon Strip-Mine", 16: "Granny Canyon", 17: "Dodge City",
          18: "Tazland A-maze-ment", 19: "Disco Volcano", 20: "The Hindenbird"}


def level_name(n):
    return LEVELS.get(n, f"level {n}")


def find_messages(p):
    """Every live subtitle that has a page the game still owns."""
    out = []
    for n, o, _ in list_walk(p)[0]:
        if not (o and ee(o)):
            continue
        page = p.r32(o + O_PAGE)
        if ee(page) and p.r32(page + PAGE_CB) == END_MESSAGE:
            out.append((n, o, page))
    return out


def steal_from(p, node, obj, page, hold=15.0, log=print):
    """Take a real message's node and object without the player noticing.

    With the stub installed the message runs its full duration and the game
    destroys its own page properly -- only remove_first and free are skipped.
    Returns the record to store, or None.
    """
    for name, blk in (("node", node), ("object", obj)):
        ok, why = validate(p, blk)
        if not ok:
            log(f"    the live {name} 0x{blk:08X} does not validate: {why}")
            return None
    key = "A" if p.r32(obj + O_FLAGS) & 2 else "B"
    if stub_ok(p, key):
        p.w32(page + PAGE_CB, stub_addrs()[key])
        good = p.r32(page + PAGE_CB) == stub_addrs()[key]
    else:
        p.w32(page + PAGE_ARG, 0)
        good = p.r32(page + PAGE_ARG) == 0
        log("    no stub installed -- disarmed instead (the slot will jam)")
    if not good:
        log("    the write did not stick")
        return None

    end = time.time() + hold
    while time.time() < end and p.f32(obj + O_DURATION) > 0.0:
        time.sleep(0.006)
    detach(p, node)
    p.w32(obj + O_PAGE, 0)

    freed, end = False, time.time() + 4.0
    while time.time() < end and not freed:
        freed = not slot_state(p, key)["open"]
        time.sleep(0.02)
    if not freed:
        unjam(p, key, only_page=page, quiet=True)

    for name, blk in (("node", node), ("object", obj)):
        ok, why = validate(p, blk)
        if not ok:
            log(f"    {name} was freed anyway: {why}")
            return None
    rec = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "node": node, "obj": obj, "from_page": page,
           "level": p.r32(LEVEL_ID), "slot": key, "freed_itself": freed,
           "node_size": p.r32(node - HDR + H_SIZE),
           "obj_size": p.r32(obj - HDR + H_SIZE)}
    save({**load(), "stolen": rec})
    return rec


def cmd_steal(p, args):
    if args.bait and not bait(p):
        return 1
    print(f"    waiting up to {args.wait}s for a subtitle with a page...")
    end, got = time.time() + args.wait, None
    while time.time() < end and not got:
        m = find_messages(p)
        if m:
            got = m[0]
        time.sleep(0.01)
    if not got:
        print("    None appeared with a page attached.")
        return 1
    node, obj, page = got
    for name, blk in (("node", node), ("object", obj)):
        ok, why = validate(p, blk)
        if not ok:
            print(f"    the live {name} 0x{blk:08X} does not validate: {why}")
            return 1
    key = "A" if p.r32(obj + O_FLAGS) & 2 else "B"
    idx = p.r32(obj + O_INDEX)
    print(f"    node 0x{node:08X}  object 0x{obj:08X}  page 0x{page:08X}  "
          f"slot {key}")
    print(f"    id {idx}  {entry_text(p, idx)!r}")

    # With the stub installed the real message tears itself down properly --
    # close_page runs, the slot releases itself, nothing leaks -- and only
    # remove_first and free are skipped. That is strictly better than
    # disarming, which leaves the slot jammed and the page stranded.
    use_stub = stub_ok(p, key)
    if use_stub:
        p.w32(page + PAGE_CB, stub_addrs()[key])
        good = p.r32(page + PAGE_CB) == stub_addrs()[key]
        print(f"    redirected page+0x1D8 to the slot {key} stub")
    else:
        p.w32(page + PAGE_ARG, 0)
        good = p.r32(page + PAGE_ARG) == 0
        print("    no stub installed -- disarmed page+0x1DC instead "
              "(the slot will jam)")
    if not good:
        print("    The write did not stick. Aborting.")
        return 1

    # Same discipline as a normal run: off the list the moment the timer
    # expires, or the tick opens a second, armed page for this object.
    end = time.time() + args.hold
    while time.time() < end and p.f32(obj + O_DURATION) > 0.0:
        time.sleep(0.006)
    detach(p, node)
    p.w32(obj + O_PAGE, 0)

    freed, end = False, time.time() + 4.0
    while time.time() < end:
        if not slot_state(p, key)["open"]:
            freed = True
            break
        time.sleep(0.02)
    if not freed:
        unjam(p, key, only_page=page, quiet=True)

    n_ok, n_why = validate(p, node)
    o_ok, o_why = validate(p, obj)
    if not (n_ok and o_ok):
        print(f"    node   {'ok' if n_ok else n_why}")
        print(f"    object {'ok' if o_ok else o_why}")
        print("    Something was freed anyway. Reload the savestate.")
        return 1
    save({**load(), "stolen": {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node": node, "obj": obj, "from_page": page,
        "node_size": p.r32(node - HDR + H_SIZE),
        "obj_size": p.r32(obj - HDR + H_SIZE)}})
    print(f"    slot {key} {'released itself' if freed else 'had to be forced'}"
          + ("   <- close_page ran, so the stub works" if freed and use_stub
             else ""))
    print("    Both blocks validate and are still in use. Yours.")
    print()
    print("    SAVE STATE NOW. This one has the stolen pair and the stub in")
    print("    it, so reloading it puts you straight back here.")
    return 0


def cmd_stub(p, args):
    base = args.base if args.base is not None else \
        load().get("stub_base", STUB_DEFAULT)
    addrs = stub_addrs(base)
    if args.remove:
        for k, a in addrs.items():
            for i in range(8):
                p.w32(a + 4 * i, 0)
        print(f"    stubs at 0x{base:08X} zeroed")
        return 0
    if args.verify:
        for k, a in addrs.items():
            print(f"    slot {k} stub 0x{a:08X}: "
                  f"{'intact' if stub_ok(p, k, base) else 'ABSENT/DAMAGED'}")
        return 0

    if not scratch_idle(p, base, 0x80):
        print(f"    0x{base:08X} is not 0x80 bytes of idle zeros. Pick another")
        print("    with --base, or check it with the probe's dump first.")
        return 1
    for k, a in addrs.items():
        for i, w in enumerate(stub_words(SLOTS[k])):
            p.w32(a + 4 * i, w)
    save({**load(), "stub_base": base})
    print(f"    written at 0x{base:08X} (slot A) and 0x{addrs['B']:08X} (slot B)")
    for k, a in addrs.items():
        ok = stub_ok(p, k, base)
        print(f"      slot {k} 0x{a:08X}  {'verified' if ok else 'READBACK FAILED'}")
        if not ok:
            return 1
    print()
    print("      move  $a1, $a0            a1 = page")
    print(f"      lui   $a0, 0x{SLOTS['A'] >> 16:04X}")
    print(f"      ori   $a0, $a0, 0x{SLOTS['A'] & 0xFFFF:04X}      a0 = slot")
    print(f"      j     0x{CLOSE_PAGE:08X}         tail call close_page")
    print("      addiu $a2, $zero, 1       delay slot, a2 = 1")
    print()
    print("    SAVE STATE. The next teardown is the first time the game")
    print("    executes memory we wrote. If the recompiler will not take it,")
    print("    it hangs there -- reload and use `run --mode disarm`.")
    return 0


def cmd_run(p, args):
    st = stolen(p)
    if not st:
        return 1
    print(f"    id {args.id}  {entry_text(p, args.id)!r}  {args.seconds}s  "
          f"slot {'A' if args.flags & 2 else 'B'}  mode {args.mode}")
    r = one_run(p, st, args.id, args.seconds, args.flags, args.mode)
    if r.get("error"):
        print(f"    {r['error']}")
        return 1
    print(f"      page 0x{r['page']:08X} opened after {r['open_delay']:.2f}s"
          + ("  (queued behind another message)" if r["queued"] else ""))
    print(f"      slot {'released itself' if r['freed_itself'] else 'had to be forced'}"
          + ("  <- close_page ran" if r["freed_itself"] else
             "  <- close_page did NOT run"))
    print(f"      heap cost {r['cost']:#x} bytes over {r['total']:.1f}s")
    show_block(p, "node", st["node"], st.get("node_size"))
    show_block(p, "object", st["obj"], st.get("obj_size"))
    print("\n    Both blocks still ours." if r["ok"] else f"\n    LOST: {r['why']}")
    return 0 if r["ok"] else 1


def cmd_cycle(p, args):
    st = stolen(p)
    if not st:
        return 1
    print(f"    {args.n} rounds, id {args.id}, {args.seconds}s, mode {args.mode}")
    base, costs, freed = p.r32(IN_USE_TOTAL), [], 0
    for i in range(1, args.n + 1):
        r = one_run(p, st, args.id, args.seconds, args.flags, args.mode,
                    log=lambda s: None)
        if r.get("error"):
            print(f"    round {i}: {r['error']}")
            return 1
        costs.append(r["cost"])
        freed += r["freed_itself"]
        print(f"    round {i:3d}  page 0x{r['page']:08X}  cost {r['cost']:+#8x}  "
              f"slot {'self' if r['freed_itself'] else 'FORCED'}  "
              f"{'ok' if r['ok'] else 'LOST: ' + str(r['why'])}")
        if not r["ok"]:
            return 1
    total = (p.r32(IN_USE_TOTAL) - base) & 0xFFFFFFFF
    print(f"\n    {args.n} rounds   slot released itself {freed}/{args.n}")
    print(f"    heap {total:#x} total, {total // max(args.n, 1)} per message")
    return 0


def cmd_drop(p, args):
    st = load().get("stolen")
    if st and detach(p, st["node"]):
        p.w32(st["obj"] + O_PAGE, 0)
        print("    our node detached from list A")
    else:
        print("    our node was not on list A")
    nodes, count = list_walk(p)
    print(f"    list A now: count {count}, {len(nodes)} node(s)")
    return 0


def cmd_unjam(p, args):
    st = load().get("stolen")
    only = None if args.force else (st or {}).get("last_page")
    for k in ([args.slot.upper()] if args.slot else ["A", "B"]):
        if not unjam(p, k, only_page=only):
            s = slot_state(p, k)
            if s["open"]:
                print(f"    slot {k} busy with 0x{s['open']:08X}; "
                      "--force to clear it anyway")
            else:
                print(f"    slot {k} already free")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sub.add_parser("drop").set_defaults(fn=cmd_drop)

    u = sub.add_parser("unjam")
    u.add_argument("--slot", choices=list("ABab"))
    u.add_argument("--force", action="store_true",
                   help="clear the slot even if the page is not ours")
    u.set_defaults(fn=cmd_unjam)

    w = sub.add_parser("watch")
    w.add_argument("--wait", type=float, default=60.0)
    w.set_defaults(fn=cmd_watch)

    s = sub.add_parser("steal")
    s.add_argument("--wait", type=float, default=120.0)
    s.add_argument("--hold", type=float, default=15.0)
    s.add_argument("--bait", action="store_true",
                   help="set this level's sandwiches to 99 first")
    s.set_defaults(fn=cmd_steal)

    t = sub.add_parser("stub")
    t.add_argument("--base", type=lambda x: int(x, 0), default=None)
    t.add_argument("--verify", action="store_true")
    t.add_argument("--remove", action="store_true")
    t.set_defaults(fn=cmd_stub)

    for name, fn in (("run", cmd_run), ("cycle", cmd_cycle)):
        c = sub.add_parser(name)
        if name == "run":
            c.add_argument("id", type=int)
        else:
            c.add_argument("n", type=int)
            c.add_argument("--id", type=int, default=1339)
        c.add_argument("--seconds", type=float, default=3.0)
        c.add_argument("--flags", type=lambda x: int(x, 0), default=2)
        c.add_argument("--mode", choices=("stub", "disarm"), default="stub")
        c.set_defaults(fn=fn)

    args = ap.parse_args()
    return args.fn(Pine().connect(), args)


if __name__ == "__main__":
    sys.exit(main())
