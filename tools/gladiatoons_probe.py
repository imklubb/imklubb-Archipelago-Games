#!/usr/bin/env python3
"""
gladiatoons_probe.py -- find out what actually decides the Gladiatoons fight.

One file, no dependencies, no apworld import. Drop it anywhere, boot Taz in
PCSX2 with PINE on (Settings -> Advanced -> Enable PINE, slot 28011) and run:

    python gladiatoons_probe.py check          is anything connected, and sane
    python gladiatoons_probe.py anchor         Taz's OFFSET from the base
    python gladiatoons_probe.py watch ...      follow chains live
    python gladiatoons_probe.py endwatch       what changes when the fight ends
    python gladiatoons_probe.py compare        what a win did that a loss did not
    python gladiatoons_probe.py poke A V       does a big number end the match?

    python gladiatoons_probe.py find           address search, if anchoring fails
    python gladiatoons_probe.py ptrscan        pointer search, ditto

Everything it learns goes into gladiatoons_probe.json beside the script, so
the modes hand off to each other and a reload does not lose the search.

WHAT IS ALREADY KNOWN, AND WHY ANCHOR IS THE RIGHT SEARCH
---------------------------------------------------------
The scores move every load, so an address is only good for the load it was
found in. Two of them are on record from one such load:

    0x008277FC   Taz
    0x0088376C   Daffy      = [0x3FF064] + 0x678C

Daffy resolves off the pointer at 0x3FF064. Taz has never resolved off
anything, and the scans that looked for him could not have succeeded: they
searched for a pointer within +/-0x8000 of his address, while the separation
between the two scores is

    0x0088376C - 0x008277FC = 0x5BF70

so from that same base Taz sits at -0x557E4 -- forty times outside the widest
window those scans could report, and in the negative direction.

`anchor` searches OFFSETS from that live base rather than addresses, which is
the thing that survives a reload. It re-reads the base every round, so
reloading the fight mid-search does not spoil it; it sharpens it, because a
wrong offset cannot follow the base.

The three counters belonging to bosses that are already solved are static and
adjacent -- 0x0037D8FC (Elephant Pong), 0x00380E28 (the Gladiatoons clock),
0x00383EB0 (Disco Volcano) -- which is why `endwatch` watches that band as
well as the band the Gladiatoons scoreline lives in. A result flag could be
beside either.
"""

import argparse
import json
import os
import socket
import struct
import sys
import time
from platform import system

# ---------------------------------------------------------------- landmarks

EE_MIN, EE_MAX = 0x00100000, 0x02000000

GAME_STATE    = 0x3FF040
LEVEL_ID      = 0x3FF048
PRIOR_STATE   = 0x3FF050
LEVEL_SECONDS = 0x3FF058
TAZ_PTR       = 0x3FF060
SCORE_BASE    = 0x3FF064          # the base Daffy's score resolves off

O_STATE_PTR = 0x1C8               # Taz -> state object
S_STATE     = 0x0B0               # the state byte inside it
O_COSTUME_PTR = 0x1CC             # Taz -> costume object
C_COSTUME     = 0x11C             # the costume byte inside it
COSTUME_NONE  = 0xFF

# Every state with a name. 0x51 and 0x52 were found in Taz: Haunted -- the
# mouse and the ball are STATES, not costumes; the costume byte stays 0xFF
# through both.
STATE_NAMES = {
    0x00: "(zero -- object half built, or nothing)",
    0x0A: "idle", 0x0B: "bite",
    0x0C: "spin", 0x0D: "spin", 0x0E: "spin",
    0x2C: "DROWNED", 0x2D: "FELL out of the world",
    0x3D: "VOID-OUT", 0x3E: "CRUSHED",
    0x4F: "eating dynamite",
    0x51: "MOUSE", 0x52: "BALL",
    0x5D: "(seen entering the mouse)",
    0x59: "CAUGHT by a keeper", 0x5A: "lost a boss fight",
}
DEATH_STATES = {0x2C, 0x2D, 0x3D, 0x3E, 0x59, 0x5A}
TRANSFORM_STATES = {0x51, 0x52}
S_REQUEST = 0x10C                 # the state Taz is ASKING for
CAUGHT, DROWN, FALL, VOID, CRUSH = 0x59, 0x2C, 0x2D, 0x3D, 0x3E
BOSS_LEVELS = {7, 12, 17, 19, 20}


def state_name(st):
    if st is None:
        return "--"
    return f"0x{st:02X} {STATE_NAMES.get(st, '')}".rstrip()

GLAD_LEVEL  = 12
GLAD_TIMER  = 0x00380E28          # float, counts UP, fight ends near 120

# The counters the GAME keeps, as opposed to the ones the HUD draws. Found by
# fighting once to win and once to lose and diffing: these two swapped,
# 6/1 on the win and 1/6 on the loss. Static, and in the same block as every
# other boss counter -- 0x0037D8FC (Elephant Pong), 0x00383EB0 (Disco
# Volcano), 0x00380E28 (this fight's clock).
TAZ_SCORE, DAFFY_SCORE = 0x00380978, 0x0038097C

# The rest of that block, which moved during the fights but has not been
# identified. 0x00380968 moved only on the win and 0x00380980 only on the
# loss, so one of them may be the result itself.
GLAD_BLOCK = (0x00380960, 0x00380990)

# What the HUD draws. These track the numbers on screen exactly and move
# every load, which is what sent the whole search into a pointer hunt.
GLAD_HUD_COPIES = (0x008277FC, 0x0088376C)

SAVE_BASE, SAVE_STRIDE, FIRST_LEVEL = 0x400444, 0x238, 3
L_COMPLETE = 0x000


FILE_STRIDE = 0x42B4          # what the apworld measured, not a round guess


def level_block(level_id, save_file=0):
    return (SAVE_BASE + (level_id - FIRST_LEVEL) * SAVE_STRIDE
            + save_file * FILE_STRIDE)


GLAD_COMPLETE = level_block(GLAD_LEVEL) + L_COMPLETE     # 0x0040183C

# Destruction is kept twice. The save block holds the best the level has ever
# been taken to; a second array holds what the CURRENT run has managed, and it
# starts at zero every load, which is why seeding the save alone leaves the
# meter reading 0 under a best of 25%.
#
# Found by searching two levels by hand -- Ice Burg at 0x00408E00 and Zooney
# Tunes at 0x00409038 -- which are 0x238 apart, exactly the save stride, and
# sit the same 0x8784 above each level's own save block. So it is one parallel
# array and the other sixteen levels need no searching.
L_DESTRUCTION = 0x21C
L_SANDWICHES  = 0x1E4

# The message box.
#
# SHOW is a SEQUENCE NUMBER, not a flag. A real message increments it -- 1 to
# 2 was caught on camera -- and the game raises a box when it sees the number
# go up. That is why writing 1 into it works exactly once and then never
# again until a savestate resets it to 0, which is the symptom that gave it
# away. Writing current+1 works every time.
#
# SLOTS are the three string POINTERS the box reads, all three set to the same
# string by the game. FLAG is set alongside them and is probably a line or
# button count. CUR is the box's own copy of what it is currently rendering,
# which is how you can tell a stale box from a fresh one.
MSG_SLOTS = (0x00509000, 0x00509004, 0x00509008)
MSG_FLAG  = 0x0050900C
MSG_SHOW  = 0x00509010
MSG_CUR   = (0x004C5CE0, 0x004C5CE4, 0x004C5CE8)
MSG_UP    = 0x004C5CF0

# The pointer table the message box indexes into. Entries are 16 bytes --
# {wchar_t *text; uint32 len; ptr; 0} -- which is why a stride-8 reading of the
# same memory finds twice as many "entries": it is alternating between the two
# pointer fields of one struct. An index at or past STR_COUNT is exactly what
# an "OOB STRING" on screen is complaining about.
STR_TABLE  = 0x0069D250
STR_STRIDE = 0x10
STR_COUNT  = 1622
LIVE_DESTRUCTION_BASE = SAVE_BASE + 0x8784               # 0x00408BC8

LEVEL_NAMES = {
    3: "Yosemite Zoo (hub)", 4: "Ice Burg", 5: "Zooney Tunes",
    6: "Looney Lagoon", 7: "Elephant Pong", 8: "Sam Francisco (hub)",
    9: "Looningdale's", 10: "Samsonian Museum", 11: "Bank of Samerica",
    12: "Gladiatoons", 13: "Wile E. West (hub)", 14: "Taz: Haunted",
    15: "Cartoon Strip-Mine", 16: "Granny Canyon", 17: "Dodge City",
    18: "Tazland A-maze-ment", 19: "Disco Volcano", 20: "The Hindenbird",
}


# The live array is the save array shifted by exactly this much, field for
# field -- not a different struct with its own layout. Two independent
# anchors agree: Zooney Tunes' destruction is 0x00400AD0 saved and 0x00409038
# live, and its sandwich count is 0x00400A98 saved and 0x00409000 live. Both
# gaps are 0x8568.
#
# An earlier reading called the live struct's base "bounty + 0", which had the
# stride right and the PHASE wrong, so every live address got named as the
# level below it. Anchoring on the save layout instead removes the guess: an
# offset means the same thing in both copies.
LIVE_DELTA = 0x8568
LIVE_BLOCK_BASE = SAVE_BASE + LIVE_DELTA                 # 0x004089AC


def live_block(level_id, save_file=0):
    return level_block(level_id, save_file) + LIVE_DELTA


def live_destruction(level_id, save_file=0):
    return live_block(level_id, save_file) + L_DESTRUCTION


def live_sandwiches(level_id, save_file=0):
    return live_block(level_id, save_file) + L_SANDWICHES


FIELD_NAMES = {L_SANDWICHES: "sandwiches", L_DESTRUCTION: "destruction",
               0x218: "bounty"}


def _where(addr, save_file=0):
    """Name an address in terms of the per-level arrays, if it is in one."""
    for delta, what in ((0, "saved"), (LIVE_DELTA, "live")):
        off = addr - (SAVE_BASE + delta + save_file * FILE_STRIDE)
        if 0 <= off < SAVE_STRIDE * 18:
            lid = FIRST_LEVEL + off // SAVE_STRIDE
            field = off % SAVE_STRIDE
            named = FIELD_NAMES.get(field)
            return (f"{LEVEL_NAMES.get(lid, lid)} +0x{field:03X} ({what})"
                    + (f"  = {what} {named}" if named else ""))
    return ""

DEFAULT_BAND = (0x00300000, 0x00600000)
FULL_BAND = (EE_MIN, EE_MAX)
NEAR_BAND = (0x0037C000, 0x00388000)      # the three known boss counters

# What endwatch watches by default: the counter band the other two bosses
# keep their scores in, and the band the Gladiatoons scoreline actually turned
# out to live in. A result flag will be beside one or the other.
WATCH_BANDS = ((0x0037C000, 0x00388000), (0x00820000, 0x00890000))
WIDE_BAND = (0x00300000, 0x00900000)


# What one score is, relative to the other. Daffy was found at 0x0088376C and
# Taz at 0x008277FC on the same load; if the two counters live in one
# structure that separation is a constant and either one gives the other.
# Measured, not derived. Deriving Taz's offset from Daffy's by assuming the
# two counters sit a fixed distance apart gave -0x557E4, which was wrong by
# 0x50: the separation was 0x5BF70 when the two literals were recorded and
# 0x5BFC0 on the load anchor ran against. Each side gets its own offset, and
# each has to survive a reload on its own evidence.
DAFFY_BASE, DAFFY_OFF = 0x3FF064, 0x678C
TAZ_OFF = -0x55834                  # true on one load only -- see below

# Taz's counter is not at a fixed offset from anything. Measured across two
# loads, with the base read live each time:
#
#   load A   base 0x0087D030   Taz 0x008277FC   offset -0x55834   sep 0x5BFC0
#   load B   base 0x0087D340   Taz 0x00827A2C   offset -0x55914   sep 0x5C0A0
#
# Both the offset and the separation move by exactly 0xE0, and the two
# targets in taz_offsets.json (0x008277FC and 0x008278DC) are 0xE0 apart as
# well. Three independent sightings of the same number: Taz's counter is one
# record in an array of 0xE0-byte records and his slot varies per load.
RECORD_STRIDE = 0xE0
SCORE_SEPARATION = 0x5BFC0          # Daffy - Taz at the k=0 slot
DEFAULT_SCORE_EXPRS = (hex(TAZ_SCORE), hex(DAFFY_SCORE))


def _offsets(rest, expr):
    """`+0x1CC-0x10` -> 0x1BC. Hex never contains + or -, so splitting on
    them is safe."""
    off = 0
    while rest:
        sign = {"+": 1, "-": -1}.get(rest[0])
        if sign is None:
            raise ValueError(f"expected + or - in {expr!r}, got {rest[0]!r}")
        rest = rest[1:]
        j = 0
        while j < len(rest) and rest[j] not in "+-":
            j += 1
        off += sign * int(rest[:j], 0)
        rest = rest[j:]
    return off


def resolve_addr(pine, expr):
    """An address, a pointer read plus a signed offset, or a chain of them.

        0x00380978                      a literal
        [0x3FF064]+0x678C               one hop, the Gladiatoons scoreline
        [0x3FF064]-0x557E4              the same base, the other way
        [[0x3FF060]+0x1CC]+0x11C        two hops -- Taz's costume byte

    Brackets nest, so any chain the world uses can be written out. The
    costume above is the same walk `mem.deref(TAZ_PTR, O_COSTUME_PTR,
    C_COSTUME)` makes: read Taz, step to the costume object, then the field.

    Resolved fresh every time it is used, because the whole point of a chain
    is that the address it lands on moves.
    """
    s = str(expr).replace(" ", "")
    if not s.startswith("["):
        return int(s, 0)

    depth = 0
    end = None
    for i, ch in enumerate(s):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise ValueError(f"unbalanced brackets in {expr!r}")

    inner = resolve_addr(pine, s[1:end])
    if inner is None or not (EE_MIN <= inner < EE_MAX):
        return None
    p = pine.read_u32(inner)
    if not (EE_MIN <= p < EE_MAX):
        return None
    a = p + _offsets(s[end + 1:], expr)
    return a if EE_MIN <= a < EE_MAX else None


WIDTH_SUFFIXES = {":u8": "u8", ":u16": "u16", ":u32": "u32",
                  ":f32": "float", ":float": "float"}


def split_width(expr):
    """`0x507218:u32` -> ("0x507218", "u32"). None means show every width."""
    for suf, name in WIDTH_SUFFIXES.items():
        if str(expr).endswith(suf):
            return str(expr)[:-len(suf)], name
    return str(expr), None


def read_as(pine, addr, width):
    if width == "u8":
        return pine.read_u8(addr)
    if width == "u16":
        return pine.read_u16(addr)
    if width == "u32":
        return pine.read_u32(addr)
    if width == "float":
        return round(pine.read_float(addr), 3)
    return None


def parse_bands(pairs):
    """--band 0x820000 0x890000 --band 0x37C000 0x388000"""
    out = []
    for p in pairs:
        lo, hi = (int(x, 0) for x in p)
        out.append((lo, hi))
    return out


def diff_into(changes, lo, old, new, t):
    """Record every byte that moved, without walking every byte.

    A whole-slice comparison is done in C and is thousands of times faster
    than a Python loop, so the loop only ever runs over the few kilobytes that
    actually differ. Without this a 500KB band cannot be polled even once a
    second.
    """
    if old == new:
        return
    step = 1024
    for s in range(0, len(new), step):
        e = min(s + step, len(new))
        if old[s:e] == new[s:e]:
            continue
        for i in range(s, e):
            if old[i] != new[i]:
                changes.setdefault(lo + i, []).append((t, old[i], new[i]))
                old[i] = new[i]

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "gladiatoons_probe.json")


# ---------------------------------------------------------------- PINE

class Pine:
    """Just enough PINE to read and write PCSX2 memory."""

    MAX_IPC_SIZE = 650000
    READ8, READ16, READ32, WRITE8, WRITE32 = 0, 1, 2, 4, 6
    ID = 0xC

    def __init__(self, slot=28011):
        self._slot = slot
        self._sock = None

    def connect(self):
        if self._sock is not None:
            return True
        if system() == "Windows":
            family, name = socket.AF_INET, ("127.0.0.1", self._slot)
        elif system() == "Darwin":
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
            raise ConnectionError(
                f"could not reach PCSX2 on {name!r}: {e}\n"
                "Is PCSX2 running with a game booted, and PINE enabled "
                "(Settings -> Advanced) on slot 28011?") from None
        self._sock = s
        return True

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def reconnect(self):
        """After a rejected message the socket may hold half a reply, and
        every read after that is silently shifted. Start again instead."""
        self.close()
        self.connect()

    def _send(self, request):
        self._sock.sendall(request)
        want, buf = 4, b""
        while len(buf) < want:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("PCSX2 closed the connection.")
            buf += chunk
            if want == 4 and len(buf) >= 4:
                want = int.from_bytes(buf[0:4], "little")
                if want > self.MAX_IPC_SIZE:
                    raise ConnectionError("oversized PINE reply")
        if buf[4] == 0xFF:
            raise ConnectionError("PCSX2 reported a failure")
        return buf

    def _one(self, cmd, addr, extra=b""):
        body = bytes([cmd]) + addr.to_bytes(4, "little") + extra
        return self._send((len(body) + 4).to_bytes(4, "little") + body)

    def read_u8(self, addr):
        return self._one(self.READ8, addr)[5]

    def read_u16(self, addr):
        return int.from_bytes(self._one(self.READ16, addr)[5:7], "little")

    def read_u32(self, addr):
        return int.from_bytes(self._one(self.READ32, addr)[5:9], "little")

    def read_float(self, addr):
        return struct.unpack("<f", self.read_u32(addr).to_bytes(4, "little"))[0]

    def write_u8(self, addr, value):
        self._one(self.WRITE8, addr, bytes([value & 0xFF]))

    def write_u32(self, addr, value):
        self._one(self.WRITE32, addr, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def game_id(self):
        # The ID request carries no address, so it is 5 bytes, not 9. Framing
        # it like a read makes PCSX2 answer with a failure.
        r = self._send((5).to_bytes(4, "little") + bytes([self.ID]))
        return r[9:-1].decode("ascii", "replace")

    # ------------------------------------------------------------ batched

    def batch_u32(self, addresses):
        """Many 32-bit reads per round trip. The reply is one status byte
        followed by the words in order, which is how PCSX2 answers a batch."""
        if not addresses:
            return []
        body = b"".join(bytes([self.READ32]) + a.to_bytes(4, "little")
                        for a in addresses)
        reply = self._send((len(body) + 4).to_bytes(4, "little") + body)
        out, off = [], 5
        for _ in addresses:
            out.append(int.from_bytes(reply[off:off + 4], "little"))
            off += 4
        return out

    def batch_u8(self, addresses):
        if not addresses:
            return []
        body = b"".join(bytes([self.READ8]) + a.to_bytes(4, "little")
                        for a in addresses)
        reply = self._send((len(body) + 4).to_bytes(4, "little") + body)
        return list(reply[5:5 + len(addresses)])


WORDS_PER_REQUEST = 192           # raised by calibrate() to whatever holds


def calibrate(pine, quiet=False):
    """How many reads PCSX2 will take in one message.

    The client this came from sends 192 at a time, which is safe and slow --
    3MB that way is four thousand round trips. PCSX2 will normally take
    thousands per message, but "normally" is not "always" across versions, so
    it is measured rather than assumed. A rejected message can leave the
    socket mid-reply, so each failure reconnects before trying smaller.
    """
    global WORDS_PER_REQUEST
    for n in (8192, 2048, 512, 192, 64):
        try:
            addrs = list(range(0x003F0000, 0x003F0000 + 4 * n, 4))
            if len(pine.batch_u32(addrs)) == n:
                WORDS_PER_REQUEST = n
                if not quiet:
                    print(f"    batching {n} reads per request")
                return n
        except Exception:
            try:
                pine.reconnect()
            except Exception:
                pass
    WORDS_PER_REQUEST = 64
    return 64


def read_block(pine, start, size, progress=None):
    """A span of EE RAM as bytes, in address order."""
    lo = start & ~3
    hi = (start + size + 3) & ~3
    addrs = range(lo, hi, 4)
    out = bytearray()
    total = len(addrs)
    done = 0
    for i in range(0, total, WORDS_PER_REQUEST):
        chunk = list(addrs[i:i + WORDS_PER_REQUEST])
        for v in pine.batch_u32(chunk):
            out += v.to_bytes(4, "little")
        done += len(chunk)
        if progress and (i // WORDS_PER_REQUEST) % 8 == 0:
            progress(done, total)
    if progress:
        progress(total, total)
    return bytes(out[start - lo:start - lo + size])


def _bar(done, total):
    pct = 100.0 * done / total if total else 100.0
    sys.stdout.write(f"\r    reading... {pct:5.1f}%  ")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


# ---------------------------------------------------------------- saved state

def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


QUIET_SAVE = [False]


def save_state(st):
    with open(STATE_PATH, "w") as fh:
        json.dump(st, fh, indent=2)
    if not QUIET_SAVE[0]:
        print(f"    saved to {STATE_PATH}")


# ---------------------------------------------------------------- widths

# Each width knows how to turn a number into the bytes it would occupy, and
# what alignment to trust. Searching all four at once costs almost nothing and
# means a score stored as a word or a float is not missed by assuming a byte.
WIDTHS = {
    "u8":    (1, lambda v: bytes([v & 0xFF]) if 0 <= v < 256 else None),
    "u16":   (2, lambda v: struct.pack("<H", v) if 0 <= v < 65536 else None),
    "u32":   (4, lambda v: struct.pack("<I", v) if 0 <= v < 1 << 32 else None),
    "float": (4, lambda v: struct.pack("<f", float(v))),
}


def find_in_block(buf, base, value, widths=None):
    """Every address in buf holding `value`, at any of the widths.

    Returns {"u8": [addrs], "u16": [...], ...}. Alignment is enforced for the
    multi-byte widths: a score that straddles a word boundary is not a thing
    this game does, and not enforcing it triples the false positives.
    """
    out = {}
    for name in (widths or WIDTHS):
        size, pack = WIDTHS[name]
        needle = pack(value)
        if needle is None:
            out[name] = []
            continue
        hits, pos = [], 0
        while True:
            pos = buf.find(needle, pos)
            if pos < 0:
                break
            if size == 1 or (base + pos) % size == 0:
                hits.append(base + pos)
            pos += 1
        out[name] = hits
    return out


def read_candidates(pine, cands):
    """Re-read a candidate map {width: [addrs]} and return {width: {addr: v}}."""
    out = {}
    for name, addrs in cands.items():
        if not addrs:
            out[name] = {}
            continue
        size, _ = WIDTHS[name]
        vals = {}
        if size == 1:
            for i in range(0, len(addrs), WORDS_PER_REQUEST):
                chunk = addrs[i:i + WORDS_PER_REQUEST]
                for a, v in zip(chunk, pine.batch_u8(chunk)):
                    vals[a] = v
        else:
            # u16 and u32 and float all live inside aligned words; read the
            # containing word once and cut the value back out of it.
            words = sorted({a & ~3 for a in addrs})
            raw = {}
            for i in range(0, len(words), WORDS_PER_REQUEST):
                chunk = words[i:i + WORDS_PER_REQUEST]
                for a, v in zip(chunk, pine.batch_u32(chunk)):
                    raw[a] = v.to_bytes(4, "little")
            for a in addrs:
                w = raw.get(a & ~3)
                if w is None:
                    continue
                off = a & 3
                if name == "u16":
                    vals[a] = struct.unpack_from("<H", w, off)[0]
                elif name == "u32":
                    vals[a] = struct.unpack_from("<I", w, off)[0]
                else:
                    vals[a] = struct.unpack_from("<f", w, off)[0]
        out[name] = vals
    return out


def matches(name, got, want):
    if name == "float":
        return abs(got - float(want)) < 1e-3
    return got == want


def count(cands):
    return sum(len(v) for v in cands.values())


def show(cands, limit=12):
    for name, addrs in cands.items():
        if not addrs:
            continue
        head = ", ".join(f"0x{a:08X}" for a in sorted(addrs)[:limit])
        more = "" if len(addrs) <= limit else f"  (+{len(addrs) - limit} more)"
        print(f"      {name:<6} {len(addrs):>7}   {head}{more}")


# ---------------------------------------------------------------- modes

def landmarks(pine):
    """Everything already known, read once. The shape of a sane read."""
    lid = pine.read_u32(LEVEL_ID)
    rows = [
        ("level id", f"{lid}" + ("  <- Gladiatoons" if lid == GLAD_LEVEL else "")),
        ("game state", f"0x{pine.read_u32(GAME_STATE):08X}"),
        ("prior state", f"0x{pine.read_u32(PRIOR_STATE):08X}"),
        ("level seconds", f"{pine.read_u32(LEVEL_SECONDS)}"),
        ("clock 0x380E28", f"{pine.read_float(GLAD_TIMER):.3f}"),
    ]
    taz = pine.read_u32(TAZ_PTR)
    rows.append(("Taz pointer", f"0x{taz:08X}"
                 + ("" if EE_MIN <= taz < EE_MAX else "   (null -- loading?)")))
    st = None
    if EE_MIN <= taz < EE_MAX:
        so = pine.read_u32(taz + O_STATE_PTR)
        if EE_MIN <= so < EE_MAX:
            st = pine.read_u8(so + S_STATE)
            rows.append(("Taz state", f"0x{st:02X}"))
    sus = pine.read_u32(DAFFY_BASE)
    rows.append((f"[0x{DAFFY_BASE:X}]", f"0x{sus:08X}"
                 + ("   base pointer" if EE_MIN <= sus < EE_MAX
                    else "   NOT an EE pointer")))
    if EE_MIN <= sus < EE_MAX:
        for label, off in (("Daffy", DAFFY_OFF), ("Taz", TAZ_OFF)):
            a = sus + off
            label = f"{label:<6}{off:+#x}"
            rows.append((f"  {label}", f"0x{a:08X} = {pine.read_u8(a)}"
                         if EE_MIN <= a < EE_MAX else "out of range"))
    rows.append(("--- the real pair", "---"))
    for a, who in ((TAZ_SCORE, "Taz"), (DAFFY_SCORE, "Daffy")):
        rows.append((f"score {who}", f"0x{a:08X} = {pine.read_u8(a)}"))
    lo, hi = GLAD_BLOCK
    blk = read_block(pine, lo, hi - lo)
    for off in range(0, min(len(blk), 48), 16):
        rows.append((f"0x{lo + off:08X}",
                     " ".join(f"{b:02X}" for b in blk[off:off + 16])))
    # What the HUD draws. Right on the load they were found on and wrong
    # after it; kept only so the difference is visible.
    for a, who in zip(GLAD_HUD_COPIES, ("Taz", "Daffy")):
        rows.append((f"HUD copy {who}", f"0x{a:08X} = {pine.read_u8(a)}"))
    rows.append(("complete(12)", f"{pine.read_u32(GLAD_COMPLETE)}"
                 f"   @0x{GLAD_COMPLETE:08X}"))
    for k, v in rows:
        print(f"    {k:<16} {v}")
    return lid, st


def cmd_check(pine, args):
    try:
        print(f"    game id: {pine.game_id()}")
    except Exception as e:
        print(f"    game id unavailable ({e}) -- reads may still be fine")
    print()
    landmarks(pine)
    print()
    print("    The pair to trust is `score Taz` and `score Daffy`. During a")
    print("    fight they should match the HUD; if they do, nothing here")
    print("    needs a pointer, a chain or a runtime search ever again.")
    return 0


def cmd_find(pine, args):
    """Narrow the whole band down to the two addresses, by value.

    Ordinary exact-value search, the same thing Cheat Engine does, except it
    is driven from here so the band can be the one the other bosses live in
    rather than all of RAM. Three rounds with different scores is normally
    enough to go from tens of thousands of hits to one.
    """
    band = FULL_BAND if args.full else (NEAR_BAND if args.near else DEFAULT_BAND)
    lo, hi = band
    print(f"    band 0x{lo:08X}-0x{hi:08X}  ({(hi - lo) / 1048576:.1f} MB)")
    if pine.read_u32(LEVEL_ID) != GLAD_LEVEL:
        print("    WARNING: the level id is not 12. Start the Gladiatoons")
        print("    fight first -- searching from the hub finds nothing.")
    print()
    print("    Play the fight. Whenever the scoreline CHANGES, type it here")
    print("    as `taz daffy`, e.g. `2 1`, and press Enter. `q` when done.")
    print("    Do not pause the emulator -- PINE is served by the EE thread")
    print("    and a paused core stops answering.")
    print()

    taz_c = daffy_c = None
    rounds = 0
    while True:
        try:
            line = input("    scores (taz daffy) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line in ("q", "quit", "done", ""):
            break
        parts = line.split()
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            print("      want two whole numbers, e.g. `3 1`")
            continue
        tv, dv = int(parts[0]), int(parts[1])

        t0 = time.time()
        if taz_c is None:
            buf = read_block(pine, lo, hi - lo, progress=_bar)
            taz_c = find_in_block(buf, lo, tv)
            daffy_c = find_in_block(buf, lo, dv)
            del buf
        else:
            for cands, want in ((taz_c, tv), (daffy_c, dv)):
                got = read_candidates(pine, cands)
                for name in list(cands):
                    cands[name] = [a for a in cands[name]
                                   if a in got[name]
                                   and matches(name, got[name][a], want)]
        rounds += 1
        print(f"    round {rounds}  ({time.time() - t0:.1f}s)")
        print(f"      Taz = {tv}:   {count(taz_c)} candidates")
        show(taz_c)
        print(f"      Daffy = {dv}: {count(daffy_c)} candidates")
        show(daffy_c)
        print()
        if 0 < count(taz_c) <= 4 and 0 < count(daffy_c) <= 4:
            print("      Down to a handful. One more round with a different")
            print("      scoreline settles it; then run `verify` after a reload.")
            print()
        if count(taz_c) == 0 or count(daffy_c) == 0:
            print("      Nothing left. Either a score was typed that was not")
            print("      on screen at the moment of the read, or the value is")
            print("      not stored in this band -- try again with --full.")
            print()
            break

    st = load_state()
    st["band"] = [lo, hi]
    st["taz"] = {k: v for k, v in (taz_c or {}).items() if v}
    st["daffy"] = {k: v for k, v in (daffy_c or {}).items() if v}
    st["found_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    return 0


def cmd_anchor(pine, args):
    """Find a score's OFFSET from a live base pointer, not its address.

    This is the search that should have been run instead of scanning address
    space. Daffy already resolves as [0x3FF064] + 0x678C, so the base is
    known; what is wanted for Taz is the offset from that same base, and an
    offset is the thing that survives a reload.

    The base is re-read every round, so if the fight reloads mid-search the
    candidates are re-anchored automatically -- a reload in the middle makes
    the answer STRONGER, because a wrong offset cannot follow the base.

    The old scan could not have found Taz at any window it used: Daffy sits
    0x678C above the base and Taz 0x557E4 below it, and the window was
    +/-0x8000.
    """
    window = int(args.window, 0)
    base_at = int(args.base, 0)
    cands, rounds = None, 0
    print(f"    base pointer at 0x{base_at:08X}, window +/-0x{window:X}")

    # If a previous run left offsets, say what they read right now before
    # searching anything. If one of them already matches the screen there is
    # nothing to search for on this load.
    prior = load_state().get(f"anchor_{args.who}", [])
    if prior and not args.fresh:
        b = pine.read_u32(base_at)
        if EE_MIN <= b < EE_MAX:
            print(f"    saved offsets, read against base 0x{b:08X} right now:")
            for w, offs in prior[-1]["offsets"].items():
                for o in offs[:8]:
                    a = b + o
                    v = pine.read_u8(a) if EE_MIN <= a < EE_MAX else "??"
                    print(f"      {w:<6} {o:+#x}  @0x{a:08X} = {v}")
            print("    If one of those is already the number on screen, this")
            print("    search is done -- press q.")
    print()
    print("    Type the score on screen for the side you are hunting each")
    print("    time it changes. `q` to stop.")
    print()
    while True:
        try:
            line = input(f"    {args.who}'s score now > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line in ("q", "quit", "done", ""):
            break
        if not line.lstrip("-").isdigit():
            print("      a whole number, please")
            continue
        want = int(line)

        base = pine.read_u32(base_at)
        if not (EE_MIN <= base < EE_MAX):
            print(f"      [0x{base_at:08X}] = 0x{base:08X}, not an EE pointer "
                  "-- are you in the fight?")
            continue
        known = pine.read_u8(base + args.known_offset) \
            if EE_MIN <= base + args.known_offset < EE_MAX else None
        print(f"      base = 0x{base:08X}   "
              f"+0x{args.known_offset:X} reads {known}")

        if cands is None:
            lo = max(EE_MIN, base - window)
            hi = min(EE_MAX, base + window)
            buf = read_block(pine, lo, hi - lo, progress=_bar)
            hits = find_in_block(buf, lo, want)
            cands = {w: [a - base for a in addrs] for w, addrs in hits.items()}
            del buf
        else:
            addrs = {w: [base + o for o in offs] for w, offs in cands.items()}
            got = read_candidates(pine, addrs)
            cands = {w: [o for o in offs
                         if (base + o) in got[w]
                         and matches(w, got[w][base + o], want)]
                     for w, offs in cands.items()}
        rounds += 1
        print(f"    round {rounds}: {count(cands)} offset(s) still fit")
        for w, offs in cands.items():
            if not offs:
                continue
            head = ", ".join(("+" if o >= 0 else "-") + f"0x{abs(o):X}"
                             for o in sorted(offs, key=abs)[:14])
            extra = "" if len(offs) <= 14 else f"  (+{len(offs) - 14} more)"
            print(f"      {w:<6} {len(offs):>7}   {head}{extra}")
        print()
        if count(cands) == 0:
            print("      Nothing left. Either the value typed was not on")
            print("      screen at the moment of the read, or the score is")
            print("      further from the base than the window -- widen it.")
            break

    st = load_state()
    key = f"anchor_{args.who}"
    runs = [] if args.fresh else st.get(key, [])
    runs.append({"base_at": base_at, "known_offset": args.known_offset,
                 "offsets": {w: o for w, o in (cands or {}).items() if o},
                 "when": time.strftime("%Y-%m-%d %H:%M:%S")})
    st[key] = runs[-6:]

    # One search inside one load proves the offset fits that load. Only the
    # offsets that come back from a SEPARATE run, after a reload, are the
    # answer -- which is the mistake the whole score hunt has been making.
    if len(runs) > 1:
        common = None
        for r in runs:
            flat = {(w, o) for w, offs in r["offsets"].items() for o in offs}
            common = flat if common is None else (common & flat)
        print(f"    across {len(runs)} runs, {len(common)} offset(s) survived:")
        for w, o in sorted(common, key=lambda x: abs(x[1])):
            print(f"      {w:<6} {o:+#x}")
        if not common:
            print("      None. The base at "
                  f"0x{base_at:08X} is not what the score hangs off.")
        st[key + "_survived"] = sorted(f"{w}:{o:+#x}" for w, o in common)
    save_state(st)
    if len(runs) == 1:
        print("    That is one load. Reload the fight and run this again")
        print("    WITHOUT deleting the json -- only an offset that comes")
        print("    back a second time is real.")
    return 0


def cmd_track(pine, args):
    """Narrow a band down to one address, by typing what is on screen.

    The same search `find` does, for a single number rather than a scoreline.
    Anything the game shows and the player can read -- a destruction
    percentage, a bounty, a timer -- can be found this way in three or four
    rounds.
    """
    band = FULL_BAND if args.full else (NEAR_BAND if args.near else DEFAULT_BAND)
    lo, hi = band
    print(f"    band 0x{lo:08X}-0x{hi:08X}  ({(hi - lo) / 1048576:.1f} MB)")
    print(f"    Type {args.what} as it reads on screen, each time it changes.")
    print("    `q` when done.")
    print()
    cands, rounds = None, 0
    while True:
        try:
            line = input(f"    {args.what} > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line in ("q", "quit", "done", ""):
            break
        try:
            want = int(float(line))
        except ValueError:
            print("      a number, please")
            continue
        t0 = time.time()
        if cands is None:
            buf = read_block(pine, lo, hi - lo, progress=_bar)
            cands = find_in_block(buf, lo, want)
            del buf
        else:
            got = read_candidates(pine, cands)
            for name in list(cands):
                cands[name] = [a for a in cands[name]
                               if a in got[name]
                               and matches(name, got[name][a], want)]
        rounds += 1
        print(f"    round {rounds} ({time.time() - t0:.1f}s): "
              f"{count(cands)} candidate(s)")
        show(cands)
        print()
        if count(cands) == 0:
            print("      Nothing left -- either the value typed was not on")
            print("      screen at the moment of the read, or it is stored")
            print("      somewhere outside this band. Try --full.")
            break
    st = load_state()
    st[f"track_{args.what}"] = {k: v for k, v in (cands or {}).items() if v}
    st[f"track_{args.what}_when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    return 0


def cmd_sandwiches(pine, args):
    """Every level's sandwich count, and set one.

    99 is the useful number: one more collected fires the game's own
    hundred-sandwich message, which is the cheapest way to make a message box
    appear on demand while working out what decides WHICH message it shows.

    Close the AP client first -- it rewrites these counts itself, and the two
    of you disagreeing is how a save gets confusing.
    """
    f = int(args.save_file)
    here = pine.read_u32(LEVEL_ID)
    if args.find:
        return _sandwich_find(pine, args, f, here)
    if args.set:
        lid = int(args.level) if args.level else here
        if not (FIRST_LEVEL <= lid <= 20):
            print(f"    level {lid} is not a level.")
            return 1
        want = int(args.set)
        # Both copies. The save one is what the level was left with, the live
        # one is what the HUD is counting right now, and setting only the save
        # copy is exactly the mistake that made this look broken.
        targets = [("saved", level_block(lid, f) + L_SANDWICHES),
                   ("live ", live_sandwiches(lid, f))]
        print(f"    {LEVEL_NAMES.get(lid, lid)}")
        for what, a in targets:
            before = pine.read_u32(a)
            pine.write_u32(a, want)
            print(f"      {what}  0x{a:08X}: {before} -> {pine.read_u32(a)}")
        # A running AP client rewrites these itself. Reading back once proves
        # the write landed; reading back a second later proves it STAYED, and
        # those are different questions.
        time.sleep(1.2)
        stomped = [(w, a, pine.read_u32(a)) for w, a in targets
                   if pine.read_u32(a) != want]
        if stomped:
            print()
            for w, a, v in stomped:
                print(f"      a second later, {w.strip()} 0x{a:08X} is {v}")
            print("    Something else is writing this -- almost certainly the")
            print("    AP client's sandwich spoof. Close the client and retry.")
            return 1
        print()
        print("    Both held. Collect one more in that level to fire the")
        print("    hundred-sandwich message.")
        return 0

    print(f"    save file {f}, currently in level {here}"
          f"  ({LEVEL_NAMES.get(here, '?')})")
    print()
    print(f"      {'lid':>3}  {'level':<21} {'saved':>19}   {'live (this run)':>19}")
    vals = {}
    for lid in range(FIRST_LEVEL, 21):
        a = level_block(lid, f) + L_SANDWICHES
        la = live_sandwiches(lid, f)
        v, lv = pine.read_u32(a), pine.read_u32(la)
        vals[lid] = v
        mark = "  <-- you are here" if lid == here else ""
        print(f"      {lid:>3}  {LEVEL_NAMES.get(lid, '?'):<21} "
              f"0x{a:08X}={v:<4}  0x{la:08X}={lv:<4}{mark}")
    print()
    if sum(1 for v in vals.values() if v == 100) >= 10:
        print("    Nearly every level reads 100. That is not your save -- it is")
        print("    the AP client's spoof, which writes 100 into every level so")
        print("    the hub spawns its bonus-game portals, and restores the true")
        print("    count once Taz is moving in the matching hub. If the client")
        print("    was closed mid-spoof, the 100s are still sitting there.")
        print("    Reload your save (or reconnect the client and walk around a")
        print("    hub) before trusting these numbers.")
        print()
    print("    Set one with:  sandwiches --set 99 [--level 14]")
    print("    Find the LIVE counter with:  sandwiches --find")
    return 0


def _sandwich_find(pine, args, f, here):
    """Find the word the on-screen sandwich counter actually reads.

    The number in the save block is the count the level was LEFT with, the
    same way the save holds the best destruction ever reached rather than the
    current run's. Writing it mid-level moves nothing on screen. Rather than
    guess at an offset, this watches memory across one sandwich being picked
    up and reports every word that went up by exactly one and stayed there.
    """
    lo, hi = ((int(args.band[0], 0), int(args.band[1], 0)) if args.band
              else (0x00400000, 0x00410000))
    print(f"    in level {here} ({LEVEL_NAMES.get(here, '?')}), watching "
          f"0x{lo:08X}-0x{hi:08X}")
    if here < FIRST_LEVEL:
        print("    You are not in a level. Load one with sandwiches in it.")
        return 1
    print("    two quiet snapshots first, to learn what churns...")
    a = read_block(pine, lo, hi - lo)
    time.sleep(0.4)
    b = read_block(pine, lo, hi - lo)
    n = min(len(a), len(b)) // 4
    base = {}
    for i in range(n):
        va = struct.unpack_from("<I", a, i * 4)[0]
        if va == struct.unpack_from("<I", b, i * 4)[0]:
            base[lo + i * 4] = va
    print(f"    {len(base)} of {n} words are holding still.")
    print()
    print("    NOW collect one sandwich.")

    deadline = time.time() + float(args.timeout)
    hits = []
    while time.time() < deadline:
        time.sleep(0.25)
        c = read_block(pine, lo, hi - lo)
        found = []
        for addr, was in base.items():
            now = struct.unpack_from("<I", c, addr - lo)[0]
            if now == was + 1:
                found.append((addr, was, now))
        if not found:
            continue
        # Confirm it held rather than flickered past on its way somewhere else.
        time.sleep(0.4)
        d = read_block(pine, lo, hi - lo)
        hits = [(addr, was, now) for addr, was, now in found
                if struct.unpack_from("<I", d, addr - lo)[0] == now]
        if hits:
            break
    if not hits:
        print("    Nothing went up by exactly one. Either no sandwich was")
        print("    collected, or the counter lives outside that band -- try")
        print("    --band 0x00300000 0x00600000.")
        return 1

    print()
    print(f"    {len(hits)} word(s) went up by one and stayed:")
    print()
    want = live_sandwiches(here, f)
    for addr, was, now in sorted(hits):
        note = _where(addr, f)
        if addr == want:
            note += "   <-- the live sandwich count"
        print(f"      0x{addr:08X}  {was} -> {now}   {note}")
    print()
    if any(h[0] == want for h in hits):
        print(f"    0x{want:08X} is where this level's live count was expected,")
        print("    so the layout holds and no other level needs finding.")
    else:
        print("    None of these is where the live count was expected")
        print(f"    (0x{want:08X}). Whichever of them the HUD follows, write it")
        print("    and watch the number: `poke <addr> 99`.")
    st = load_state()
    st["sandwich_find"] = {"level": here, "hits": hits,
                           "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(st)
    return 0


def _msg_raise(pine):
    """Ask for a box by incrementing the sequence number.

    Writing a literal 1 is what the first version did, and it worked once per
    savestate and then stopped -- because the number was already 1 and the
    game only reacts to it going UP.
    """
    n = pine.read_u32(MSG_SHOW)
    pine.write_u32(MSG_SHOW, (n + 1) & 0xFFFFFFFF)
    return n, pine.read_u32(MSG_SHOW)


def cmd_message(pine, args):
    """Show or hide a message box."""
    if args.hide:
        pine.write_u32(MSG_UP, 0)
        print(f"    0x{MSG_UP:08X} <- 0    (message dismissed)")
        return 0
    was_up = pine.read_u32(MSG_UP)
    n, m = _msg_raise(pine)
    time.sleep(0.35)
    up = pine.read_u32(MSG_UP)
    print(f"    show 0x{MSG_SHOW:08X}: {n} -> {m}       "
          f"up 0x{MSG_UP:08X}: {was_up} -> {up}")
    if not up:
        print("    No box. Something already had one up, or the game is in a")
        print("    state that refuses them -- try again while Taz is standing")
        print("    in a level.")
        return 1
    print()
    print("    Dismiss it with:  message --hide")
    return 0


def cmd_msgbox(pine, args):
    """Read, and drive, the whole message box.

    With no arguments it prints the three string pointers, the flag beside
    them, the sequence number, and what the box is currently rendering -- each
    dereferenced, so the text is right there.

    --ptr points all three slots at a string that already exists and raises a
    box, which proves the mechanism without writing a byte of text anywhere.
    --text writes a string of your own into scratch memory first.
    """
    def show(label):
        print(f"    {label}")
        for i, a in enumerate(MSG_SLOTS):
            v = pine.read_u32(a)
            print(f"      slot {i}   0x{a:08X} = 0x{v:08X}{_show_str(pine, v)}")
        print(f"      flag     0x{MSG_FLAG:08X} = {pine.read_u32(MSG_FLAG)}")
        print(f"      sequence 0x{MSG_SHOW:08X} = {pine.read_u32(MSG_SHOW)}")
        for i, a in enumerate(MSG_CUR):
            v = pine.read_u32(a)
            print(f"      showing{i} 0x{a:08X} = 0x{v:08X}{_show_str(pine, v)}")
        print(f"      up       0x{MSG_UP:08X} = {pine.read_u32(MSG_UP)}")

    if args.inspect:
        return _msg_inspect(pine, int(args.inspect, 0), int(args.bytes, 0))

    if not args.ptr and not args.text:
        show("right now:")
        print()
        print("    Look at one of those pointers:  msgbox --inspect 0xADDRESS")
        print("    Re-show a string that exists:   msgbox --ptr 0xADDRESS")
        print("    Show your own words:            msgbox --text \"Hello\"")
        return 0

    if args.text:
        # Match the encoding of a pointer that is KNOWN to work rather than
        # assuming. 0x006470D0 rendered as "yes" while a UTF-16 string in the
        # same slot rendered as OOB STRING, so the two are not interchangeable
        # and guessing costs a round trip every time.
        enc = args.encoding
        if enc == "auto":
            ref = int(args.like, 0)
            enc, refs = _sniff(pine, ref)
            if enc is None:
                print(f"    0x{ref:08X} is not a string in either encoding, so")
                print("    there is nothing to copy. Look at it first with")
                print(f"    `msgbox --inspect 0x{ref:08X}`, or pass --encoding.")
                return 1
            print(f"    0x{ref:08X} holds {refs!r} as {enc}, matching that.")
        raw = (args.text.encode("ascii", "replace") + b"\0" if enc == "ascii"
               else args.text.encode("utf-16-le") + b"\0\0")
        addr = (int(args.scratch, 0) if args.scratch
                else _find_scratch(pine, len(raw) + 16))
        if addr is None:
            return 1
        raw += b"\0" * 4
        for i in range(0, len(raw), 4):
            pine.write_u32(addr + i, int.from_bytes(
                raw[i:i + 4].ljust(4, b"\0"), "little"))
        gotenc, back = _sniff(pine, addr)
        print(f"    wrote {args.text!r} to 0x{addr:08X} as {enc}, "
              f"reads back {back!r}")
        if back != args.text:
            print("    That is not what went in -- something else owns that")
            print("    memory. Pass --scratch with an address you trust.")
            return 1
    else:
        addr = int(args.ptr, 0)
        enc, s = _sniff(pine, addr)
        print(f"    0x{addr:08X} holds {s!r} ({enc})" if s else
              f"    0x{addr:08X} is not a string in either encoding "
              "-- showing it anyway")

    slots = MSG_SLOTS if args.slots == "all" else (MSG_SLOTS[int(args.slots)],)
    for a in slots:
        pine.write_u32(a, addr)
    if args.flag:
        pine.write_u32(MSG_FLAG, int(args.flag, 0))
    n, m = _msg_raise(pine)
    time.sleep(0.35)
    print(f"    sequence {n} -> {m}, up = {pine.read_u32(MSG_UP)}")
    print()
    show("after:")
    print()
    print("    Dismiss it with:  message --hide")
    return 0


def _msg_inspect(pine, addr, span=0x60):
    """Look at what a message pointer actually points AT, three ways at once.

    Hex, because a struct is obvious in hex and invisible in anything else.
    ASCII and UTF-16 side by side, because which one it is has already caused
    one wrong turn. And every word dereferenced, because if it is a descriptor
    rather than a string then the text is one hop further on.
    """
    print(f"    0x{addr:08X}, {span} bytes")
    print()
    raw = read_block(pine, addr, span)
    for off in range(0, len(raw), 16):
        chunk = raw[off:off + 16]
        hexes = " ".join(f"{b:02X}" for b in chunk)
        chars = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
        print(f"      0x{addr + off:08X}  {hexes:<47}  {chars}")
    print()
    enc, s = _sniff(pine, addr, 1)
    if s:
        print(f"    As a string it is {s!r} ({enc}).")
    else:
        print("    It is not a string at this address in either encoding, so")
        print("    it is a struct -- look for the text one hop on:")
    print()
    for off in range(0, min(span, 0x40), 4):
        v = struct.unpack_from("<I", raw, off)[0]
        note = _show_str(pine, v)
        if not note and 0 < v < 0x10000:
            note = f"  (a small number -- a length?)"
        print(f"      +0x{off:02X}  0x{v:08X}{note}")
    return 0


def _entry_lookup(pine, word, band=None, quiet=False):
    """The table entry for a word, cached, re-verified before it is trusted.

    Scanning a megabyte for "Yes" takes long enough that doing it on every
    announcement would be its own bug, but a cached address that has gone
    stale is worse than no cache -- so the cached entry has to still point at
    the word before it is used, and otherwise the scan happens again.
    """
    st = load_state()
    cache = st.get("entry_cache", {})
    c = cache.get(word)
    if c and _texty(pine, pine.read_u32(c["entry"])) == word:
        return c
    lo, hi = band or (0x00600000, 0x00700000)
    if not quiet:
        print(f"    finding {word!r} in 0x{lo:08X}-0x{hi:08X}...")
    buf = read_block(pine, lo, hi - lo, progress=None if quiet else _bar)
    pat = word.encode("utf-16-le")
    hits, pos = [], 0
    while True:
        pos = buf.find(pat, pos)
        if pos < 0:
            break
        if buf[pos + len(pat):pos + len(pat) + 2] == b"\0\0":
            hits.append(lo + pos)
        pos += 2
    for h in hits:
        for i, (v,) in enumerate(struct.iter_unpack("<I", buf)):
            if v != h:
                continue
            e = lo + i * 4
            nxt = struct.unpack_from("<I", buf, i * 4 + 4)[0]
            c = {"entry": e, "ptr": h, "word": word,
                 "len_at": e + 4 if nxt in (len(word), len(word) + 1) else None,
                 "len": nxt if nxt in (len(word), len(word) + 1) else None}
            cache[word] = c
            st["entry_cache"] = cache
            save_state(st)
            return c
    return None


def _restore_stale(pine, quiet=False):
    """Undo a repoint left behind by a run that did not finish.

    Without this, one crash mid-message leaves the game's "Yes" pointing at
    scratch memory forever, and the next thing to go wrong looks unrelated.
    """
    st = load_state()
    s = st.get("msgpoint")
    if not s:
        return False
    pine.write_u32(s["entry"], s["ptr"])
    if s.get("len_at"):
        pine.write_u32(s["len_at"], s["len"])
    st.pop("msgpoint", None)
    save_state(st)
    if not quiet:
        print(f"    (restored a repoint left over from last time: "
              f"0x{s['entry']:08X} -> {_texty(pine, s['ptr'])!r})")
    return True


def _wrote_ok(pine, addr, raw):
    """Did those exact bytes land? Not "does it read as the same string".

    _texty stops after 80 bytes, so checking a long message that way always
    reported failure and sent the allocator hunting for a buffer that was
    never the problem.
    """
    return read_block(pine, addr, len(raw)) == raw


def _announce_scratch(pine, need, reserve=0x200):
    """One buffer, found once and kept.

    Hunting for fresh scratch on every message finds a NEW address each time,
    because the last message is still sitting in the old one -- so the game
    leaks a message-sized hole in memory per announcement. Reserving a block
    up front and reusing it costs one scan for the whole session.
    """
    st = load_state()
    a, size = st.get("announce_scratch"), st.get("announce_scratch_size", 0)
    if a and need <= size:
        return a
    size = max(need, reserve)
    a = _find_scratch(pine, size)
    if a:
        st = load_state()
        st["announce_scratch"] = a
        st["announce_scratch_size"] = size
        save_state(st)
    return a


def _msg_idle(pine):
    """Is the box free? Returns (idle, why-not).

    The flag at 0x0050900C deliberately is NOT part of this. It reads -2 on a
    freshly booted game and 0 from the first message onwards, and treating -2
    as "idle" meant every check after the first message failed -- the guard
    refused to let anything through and called a normal resting state
    mid-message. Whether a box is up is what actually matters.
    """
    up = pine.read_u32(MSG_UP)
    if up:
        return False, f"a box is already up (0x{MSG_UP:08X} = {up})"
    a = pine.read_u32(MSG_SHOW)
    time.sleep(0.12)
    if pine.read_u32(MSG_SHOW) != a:
        return False, "the game is asking for a box of its own right now"
    return True, ""


def announce(pine, text, seconds=3.0, word="Yes", scratch=None, log=print,
             via=0x006470D0):
    """Show a message safely, then put everything back.

    Repointing the entry is only half of it. A box that is ALREADY on screen
    is drawing that entry, so repointing changes it live -- which is why
    doing it by hand worked. A box raised from cold resolves the slots
    first, and if they hold whatever the last experiment left behind, the
    lookup fails and OOB STRING is what appears no matter where the entry
    points. So the slots have to be set to the value that resolves to this
    word, every time, before raising.

    The order matters. Guard first, because clobbering a real message is the
    thing to avoid. Restore in a finally, because the alternative is leaving
    the game's own word pointing at our scratch buffer if anything throws.
    """
    QUIET_SAVE[0] = True            # one message should not spray the log
    _restore_stale(pine, quiet=True)
    ok, why = _msg_idle(pine)
    if not ok:
        return {"shown": False, "why": why}
    c = _entry_lookup(pine, word, quiet=True)
    if not c:
        return {"shown": False, "why": f"no table entry for {word!r}"}
    need = len(text) * 2 + 16
    if scratch is None:
        scratch = _announce_scratch(pine, need)
        if scratch is None:
            return {"shown": False, "why": "no scratch memory"}

    raw = text.encode("utf-16-le") + b"\0\0\0\0"

    def paint(at):
        for i in range(0, len(raw), 4):
            pine.write_u32(at + i, int.from_bytes(
                raw[i:i + 4].ljust(4, b"\0"), "little"))

    paint(scratch)
    if not _wrote_ok(pine, scratch, raw):
        # The reserved buffer stopped being ours. Forget it and take another
        # rather than pointing the game's table at memory that will not hold
        # a string.
        st = load_state()
        st.pop("announce_scratch", None)
        save_state(st)
        scratch = _announce_scratch(pine, need)
        if scratch is None:
            return {"shown": False, "why": "no scratch memory"}
        paint(scratch)
        if not _wrote_ok(pine, scratch, raw):
            return {"shown": False, "why": f"0x{scratch:08X} will not hold it"}

    st = load_state()
    st["msgpoint"] = dict(c, text=text)
    save_state(st)
    result = {"shown": False, "why": "", "held": 0.0, "dismissed_by": ""}
    flag0 = pine.read_u32(MSG_FLAG)
    slots0 = [pine.read_u32(a) for a in MSG_SLOTS]
    try:
        if c["len_at"]:
            pine.write_u32(c["len_at"], len(text) + (c["len"] - len(word)))
        pine.write_u32(c["entry"], scratch)
        for a in MSG_SLOTS:
            pine.write_u32(a, via)
        _msg_raise(pine)
        if not _msg_wait_up(pine, 1.5):
            result["why"] = "raised the sequence and no box appeared"
            return result
        result["shown"] = True
        end = time.time() + seconds
        while time.time() < end:
            if not pine.read_u32(MSG_UP):
                result["dismissed_by"] = "player"
                break
            time.sleep(0.05)
        else:
            result["dismissed_by"] = "timer"
        result["held"] = seconds - max(0.0, end - time.time())
        _msg_reset(pine, 0.1, flag0)
    finally:
        pine.write_u32(c["entry"], c["ptr"])
        if c["len_at"]:
            pine.write_u32(c["len_at"], c["len"])
        for a, v in zip(MSG_SLOTS, slots0):
            pine.write_u32(a, v)
        st = load_state()
        st.pop("msgpoint", None)
        save_state(st)
        back = _texty(pine, pine.read_u32(c["entry"]))
        result["restored"] = (back == word)
        result["reads"] = back
    return result


def cmd_announce(pine, args):
    """Show a message the way the client will have to, or rehearse the lot."""
    if args.test:
        return _announce_test(pine, args)
    r = announce(pine, args.text, float(args.seconds), args.word,
                 int(args.scratch, 0) if args.scratch else None,
                 via=int(args.via, 0))
    if not r["shown"]:
        print(f"    Declined: {r['why']}")
        return 1
    print(f"    shown for {r['held']:.1f}s, dismissed by {r['dismissed_by']}")
    print(f"    entry back to {r['reads']!r} -- "
          + ("restored" if r["restored"] else "NOT RESTORED"))
    return 0 if r["restored"] else 1


def _announce_test(pine, args):
    """The whole thing, end to end, including the cases that must fail."""
    word = args.word
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  --  {detail}" if detail else ""))

    print("    Taz should be standing in a level, not paused.")
    print()
    _restore_stale(pine)
    c = _entry_lookup(pine, word)
    if not c:
        print(f"    Cannot find {word!r} at all -- nothing else can be tested.")
        return 1
    entry0, ptr0 = c["entry"], pine.read_u32(c["entry"])
    print(f"    entry 0x{entry0:08X} -> 0x{ptr0:08X} {_texty(pine, ptr0)!r}")
    print()

    ok, why = _msg_idle(pine)
    check("box is idle to begin with", ok,
          why or "no box up, sequence not moving")
    if not ok:
        print("    Dismiss whatever is on screen and run this again.")
        return 1

    print()
    print("    WATCH THE SCREEN. The next box should read")
    print("      Taz found an item!")
    print("    Nothing here can read the rendered text back, so that one is")
    print("    yours to judge -- OOB STRING means the slots resolved wrong,")
    print("    not that the repoint failed.")
    print()
    via = int(args.via, 0)
    r = announce(pine, "Taz found an item!", 3.0, word, via=via)
    check("first message appears", r["shown"], r.get("why", ""))
    check("it hides itself", r.get("dismissed_by") == "timer",
          f"dismissed by {r.get('dismissed_by') or 'nothing'}")
    check("entry restored afterwards", r.get("restored"),
          f"reads {r.get('reads')!r}")
    check("restored to the SAME pointer", pine.read_u32(entry0) == ptr0,
          f"0x{pine.read_u32(entry0):08X} vs 0x{ptr0:08X}")

    time.sleep(0.5)
    r2 = announce(pine, "A second message, right after", 2.0, word, via=via)
    check("it works twice in a row", r2["shown"], r2.get("why", ""))
    check("still restored", pine.read_u32(entry0) == ptr0)

    time.sleep(0.5)
    r3 = announce(pine, "This one is far too long to fit in three characters, "
                        "which is the whole point of repointing", 2.0, word,
                  via=via)
    check("length is not a limit", r3["shown"], r3.get("why", ""))

    # Now the case that MUST be refused: something else already has a box up.
    print()
    print("    raising a box the game's own way, to see if we barge in...")
    for a in MSG_SLOTS:
        pine.write_u32(a, via)
    _msg_raise(pine)
    foreign = _msg_wait_up(pine, 1.5)
    if not foreign:
        check("could stage a competing box", False,
              "none appeared, so this case went untested")
    else:
        r4 = announce(pine, "SHOULD NOT APPEAR", 2.0, word, via=via)
        check("declines while another box is up", not r4["shown"],
              r4.get("why", "it went ahead anyway"))
        check("declining left the entry alone", pine.read_u32(entry0) == ptr0)
        _msg_reset(pine)

    time.sleep(0.3)
    ok, why = _msg_idle(pine)
    check("idle again at the end", ok, why)
    check(f"{word!r} still reads {word!r}",
          _texty(pine, pine.read_u32(entry0)) == word,
          repr(_texty(pine, pine.read_u32(entry0))))

    bad = [n for n, o, _ in results if not o]
    print()
    print(f"    {len(results) - len(bad)}/{len(results)} passed.")
    if bad:
        print("    Failed: " + ", ".join(bad))
    return 1 if bad else 0


def cmd_msgpoint(pine, args):
    """Change what a word on screen says, by repointing its table entry.

    The buffers around the message flags hold one-character strings -- '2',
    '4' -- so they are ids being fed to a lookup, not the text. The text
    itself comes from the UTF-16 tables, the ones `writetext` already edits.

    Editing in place is no good: the tables are PACKED, so "Yes" has room for
    three characters and nothing longer. But each entry is a POINTER, and a
    pointer can be aimed anywhere. Write the new string into free memory,
    aim the entry at it, and the length stops mattering.

    Since a forced box reliably shows "Yes", repointing whatever "Yes" is
    makes every forced box say whatever we like -- which is the whole
    requirement, without ever learning what picks the string.
    """
    st = load_state()
    if args.restore:
        saved = st.get("msgpoint")
        if not saved:
            print("    Nothing saved to restore.")
            return 1
        pine.write_u32(saved["entry"], saved["ptr"])
        if saved.get("len_at"):
            pine.write_u32(saved["len_at"], saved["len"])
        print(f"    0x{saved['entry']:08X} <- 0x{saved['ptr']:08X}, "
              f"back to {_texty(pine, saved['ptr'])!r}")
        st.pop("msgpoint", None)
        save_state(st)
        return 0

    word = args.word
    lo, hi = ((int(args.band[0], 0), int(args.band[1], 0)) if args.band
              else (0x00600000, 0x00700000))
    print(f"    looking for {word!r} as UTF-16LE in 0x{lo:08X}-0x{hi:08X}")
    buf = read_block(pine, lo, hi - lo, progress=_bar)
    pat = word.encode("utf-16-le")
    hits, pos = [], 0
    while True:
        pos = buf.find(pat, pos)
        if pos < 0:
            break
        # Only whole strings: a hit must be terminated, or it is the head of
        # a longer word that merely starts the same way.
        end = pos + len(pat)
        if buf[end:end + 2] == b"\0\0":
            hits.append(lo + pos)
        pos += 2
    print(f"    {len(hits)} copy(ies): "
          + ", ".join(f"0x{a:08X}" for a in hits[:8]))
    if not hits:
        print("    None. Check the spelling and the capitals -- the box")
        print("    showed 'Yes', not 'yes'. --full widens the search.")
        return 1

    # The copy that matters is the one something POINTS at: that is the table
    # entry the renderer reads. A copy nothing points at is scratch.
    entries = []
    for h in hits:
        for i, (v,) in enumerate(struct.iter_unpack("<I", buf)):
            if v == h:
                entries.append((lo + i * 4, h))
    if not entries:
        print("    Nothing in that band points at any of them, so these are")
        print("    working copies rather than table entries. Try --full.")
        return 1
    print()
    for e, h in entries[:8]:
        nxt = struct.unpack_from("<I", buf, e - lo + 4)[0] if e - lo + 8 <= len(buf) else 0
        idx = ((e - STR_TABLE) // STR_STRIDE
               if STR_TABLE <= e < STR_TABLE + STR_STRIDE * STR_COUNT else None)
        print(f"      entry 0x{e:08X} -> 0x{h:08X}   next word {nxt}"
              + (f"   index {idx}" if idx is not None else ""))
    if not args.text:
        print()
        print("    Pick one and give it new words:")
        print(f"      msgpoint --word {word!r} --text \"Your message\"")
        return 0

    entry, orig = (int(args.entry, 0), None) if args.entry else entries[0]
    if args.entry:
        orig = pine.read_u32(entry)
    print()
    print(f"    using entry 0x{entry:08X}")
    need = len(args.text) * 2 + 8
    dest = (int(args.scratch, 0) if args.scratch else _find_scratch(pine, need))
    if dest is None:
        return 1
    raw = args.text.encode("utf-16-le") + b"\0\0\0\0"
    for i in range(0, len(raw), 4):
        pine.write_u32(dest + i, int.from_bytes(
            raw[i:i + 4].ljust(4, b"\0"), "little"))
    if not _wrote_ok(pine, dest, raw):
        print(f"    0x{dest:08X} did not keep the string. Pass --scratch.")
        return 1
    print(f"    wrote {args.text!r} to 0x{dest:08X}")

    before = pine.read_u32(entry)
    len_at = len_was = None
    nxt = pine.read_u32(entry + 4)
    if nxt in (len(word), len(word) + 1):
        len_at, len_was = entry + 4, nxt
        pine.write_u32(len_at, len(args.text) + (nxt - len(word)))
        print(f"    next word was {nxt}, which is this string's length -- "
              f"set to {pine.read_u32(len_at)}")
    pine.write_u32(entry, dest)
    st["msgpoint"] = {"entry": entry, "ptr": before,
                      "len_at": len_at, "len": len_was,
                      "word": word, "text": args.text}
    save_state(st)
    print(f"    0x{entry:08X}: 0x{before:08X} -> 0x{pine.read_u32(entry):08X}")
    print()
    print("    Raise a box and read it:   message")
    print("    Put the entry back:        msgpoint --restore")
    return 0


MSG_IDLE_FLAG = 0xFFFFFFFE      # 0x0050900C on a game that has shown no box


def _msg_reset(pine, settle=0.35, flag=None):
    """Take the box down, and put the flag back to whatever it WAS.

    An earlier version wrote -2 here on the theory that -2 meant idle. It
    does not -- it is just the value on a game that has never shown a
    message, and forcing it afterwards was writing a state the game had
    moved on from. Restoring the value observed before the box went up makes
    no claim about what it means.
    """
    pine.write_u32(MSG_UP, 0)
    if flag is not None:
        pine.write_u32(MSG_FLAG, flag)
    time.sleep(settle)


def _msg_wait_up(pine, timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pine.read_u32(MSG_UP):
            return True
        time.sleep(0.02)
    return False


def _room(pine, addr, unit=1, span=0x100):
    """How many characters fit here: the string, plus the zeros after it.

    Zeros running to the end of the window mean the room was not measured,
    only bounded below -- worth saying out loud, because the whole point of
    the number is deciding what is safe to overwrite.
    """
    raw = read_block(pine, addr, span)
    end = 0
    while end + unit <= len(raw) and any(raw[end:end + unit]):
        end += unit
    tail = end
    while tail + unit <= len(raw) and not any(raw[tail:tail + unit]):
        tail += unit
    return end // unit, tail // unit, tail + unit > len(raw)


def cmd_msgtext(pine, args):
    """Put your own words in the box by overwriting what it composed.

    Every attempt so far has tried to make the game CHOOSE our string, and
    every one has run into the same wall: nobody knows what the slots mean.
    This gives up on choosing. The box composes its text into a buffer -- the
    address in 0x004C5CE0 -- and then draws from that buffer, so raising any
    message at all and immediately overwriting the buffer puts arbitrary
    words on screen without understanding the lookup at all.

    If the game re-composes every frame rather than once, --hold wins the
    race repeatedly for as long as you ask.
    """
    ref = int(args.via, 0)
    _msg_reset(pine)
    for a in MSG_SLOTS:
        pine.write_u32(a, ref)
    n, m = _msg_raise(pine)
    if not _msg_wait_up(pine):
        print(f"    Raised the sequence {n} -> {m} and no box appeared.")
        print("    Try it while Taz is standing in a level, not on a menu.")
        return 1
    buf = pine.read_u32(MSG_CUR[0])
    enc, cur = _sniff(pine, buf, 1)
    print(f"    box is up, composing into 0x{buf:08X}")
    if cur is None:
        print("    ...but that is not readable text in either encoding:")
        return _msg_inspect(pine, buf, 0x40)
    unit = 1 if enc == "ascii" else 2
    used, room, unbounded = _room(pine, buf, unit)
    if args.capacity:
        room, unbounded = int(args.capacity), False
    print(f"    it holds {cur!r} ({enc}), {used} chars, room for "
          + (f"at least {room}" if unbounded else str(room)))
    if not args.text:
        print()
        print("    Now put something there:  msgtext \"Your words\"")
        return 0
    if len(args.text) > room:
        print(f"    REFUSED: {len(args.text)} chars into room for {room}.")
        print("    Shorten it, or pass --capacity if you have measured more.")
        return 1
    raw = (args.text.encode("ascii", "replace") if enc == "ascii"
           else args.text.encode("utf-16-le"))
    raw += b"\0" * (unit * 2)

    def paint():
        for i in range(0, len(raw), 4):
            pine.write_u32(buf + i, int.from_bytes(
                raw[i:i + 4].ljust(4, b"\0"), "little"))

    paint()
    _, back = _sniff(pine, buf, 1)
    print(f"    wrote it -- buffer now reads {back!r}")
    hold = float(args.hold)
    if hold > 0:
        print(f"    holding it for {hold}s in case the game repaints...")
        end = time.time() + hold
        repaints = 0
        while time.time() < end:
            if _sniff(pine, buf, 1)[1] != args.text:
                repaints += 1
                paint()
            time.sleep(0.05)
        print(f"    put it back {repaints} time(s) -- "
              + ("the game does repaint, so the client will have to hold it"
                 if repaints else "nothing overwrote it, so one write is enough"))
    print()
    print("    Dismiss it with:  message --hide")
    return 0


def cmd_msgids(pine, args):
    """Walk numeric ids through the slots and read back what the box shows.

    0x006470D0 is not a string. In ASCII it is "4" and then binary, and it
    produced the word "yes" -- so the slots are not text pointers, and the
    obvious remaining reading is that the game turns whatever short string it
    finds into a NUMBER and looks that up. "4" would then be id 4, and a
    UTF-16 string starting with 'T' would be atoi("T") = 0, which is exactly
    the one that came back OOB.

    That is a theory, not a finding, so this tests it the cheap way: id 0
    upward, reading the composed text back out of the box's own buffer rather
    than making you watch the screen. If the first handful all render the
    same thing, the theory is wrong and it stops there instead of raising two
    hundred pointless boxes.
    """
    lo, hi = int(args.start), int(args.end)
    scratch = (int(args.scratch, 0) if args.scratch
               else _find_scratch(pine, 32))
    if scratch is None:
        return 1
    delay = float(args.delay)
    print(f"    ids {lo}..{hi}, written as ASCII to 0x{scratch:08X}")
    print()
    print(f"      {'id':>5}  {'buffer':>10}  text")
    seen, raised = [], 0
    for n in range(lo, hi + 1):
        _msg_reset(pine, delay)
        raw = str(n).encode("ascii") + b"\0\0\0\0"
        for i in range(0, len(raw), 4):
            pine.write_u32(scratch + i, int.from_bytes(
                raw[i:i + 4].ljust(4, b"\0"), "little"))
        for a in MSG_SLOTS:
            pine.write_u32(a, scratch)
        _msg_raise(pine)
        # Reading the buffer when no box came up records the LAST box's text
        # and calls it this one's, which is how the first version of this
        # managed to report six identical answers from one raise.
        if not _msg_wait_up(pine, 1.0):
            print(f"      {n:>5}  (no box)")
            seen.append((n, 0, None))
            continue
        raised += 1
        shown = pine.read_u32(MSG_CUR[0])
        _, s = _sniff(pine, shown, 1)
        seen.append((n, shown, s))
        print(f"      {n:>5}  0x{shown:08X}  {s!r}")
        if raised >= 6 and len({x[2] for x in seen if x[1]}) == 1:
            print()
            print("    Six boxes, one answer. The slots are not a number, so")
            print("    stopping rather than raising forty identical boxes.")
            return 1
    _msg_reset(pine, delay)
    if raised < 2:
        print()
        print(f"    Only {raised} box(es) actually came up out of "
              f"{len(seen)} tries, so this proved nothing either way.")
        print("    The box wants to be dismissed properly between messages;")
        print("    if it still will not repeat, try `msgtext` instead, which")
        print("    needs only one box.")
        return 1
    st = load_state()
    st["msgids"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "range": [lo, hi], "seen": seen}
    save_state(st)
    print()
    print(f"    {len({x[2] for x in seen})} distinct results across "
          f"{len(seen)} ids. Saved.")
    return 0


def _find_scratch(pine, need):
    """A run of memory nothing is using, verified twice before trusting it.

    Checked once it is merely zero right now; checked again after a pause it
    is zero that nothing is writing to, which is the difference between free
    memory and a buffer that happens to be idle.
    """
    st = load_state()
    cached = st.get("scratch_addr")
    if cached and _scratch_ok(pine, cached, need):
        print(f"    reusing scratch at 0x{cached:08X}")
        return cached
    span = max(need + 0x40, 0x200)
    print(f"    looking for {span} idle bytes...")
    # Read a megabyte at a time and find the zero runs in it, rather than
    # probing 64KB-aligned addresses one at a time -- the old way took a
    # quarter-second per candidate and gave up after checking a couple of
    # hundred addresses out of sixteen million.
    block = 0x100000
    for top in range(0x01F00000, 0x00800000, -block):
        raw = read_block(pine, top, block)
        run = start = 0
        for i, b in enumerate(raw):
            if b:
                run = 0
                continue
            if run == 0:
                start = i
            run += 1
            if run >= span:
                addr = (top + start + 15) & ~15
                if _scratch_ok(pine, addr, span):
                    st["scratch_addr"] = addr
                    save_state(st)
                    print(f"    scratch at 0x{addr:08X}")
                    return addr
                run = 0
    print("    Nowhere in 0x00800000-0x01F00000 is both empty and idle.")
    print("    Pass --scratch with an address you have checked yourself --")
    print("    `dump 0xADDRESS` twice a second apart is enough to check.")
    return None


def _scratch_ok(pine, addr, span):
    if not (EE_MIN <= addr < EE_MAX - span):
        return False
    if any(read_block(pine, addr, span)):
        return False
    time.sleep(0.25)
    return not any(read_block(pine, addr, span))


def _texty(pine, addr, want=3):
    """Read UTF-16LE at an address and return it only if it really is text.

    Fixed text bands were the wrong tool: the message box's strings turned out
    to live around 0x00646000, nowhere near the tables at 0x006A0000, and a
    band-based annotator stayed silent about the single most important value
    on the screen. Judging the bytes instead of the address finds text
    wherever the game happens to keep it.
    """
    if not (EE_MIN <= addr < EE_MAX - 0x80) or addr & 1:
        return None
    try:
        raw = read_block(pine, addr, 80)
    except Exception:
        return None
    out = []
    for i in range(0, len(raw) - 1, 2):
        lo, hi = raw[i], raw[i + 1]
        if lo == 0 and hi == 0:
            break
        if hi != 0 or lo < 0x20 or lo > 0x7E:
            return None                      # not UTF-16LE printable
        out.append(chr(lo))
    s = "".join(out)
    return s if len(s) >= want else None


def _asciiy(pine, addr, want=3):
    """Same idea as _texty, for plain single-byte strings."""
    if not (EE_MIN <= addr < EE_MAX - 0x80):
        return None
    try:
        raw = read_block(pine, addr, 64)
    except Exception:
        return None
    out = []
    for b in raw:
        if b == 0:
            break
        if b < 0x20 or b > 0x7E:
            return None
        out.append(chr(b))
    s = "".join(out)
    return s if len(s) >= want else None


def _sniff(pine, addr, want=2):
    """Read a string at an address without assuming its width.

    Assuming UTF-16 is what made the probe call 0x006470D0 "not text" when it
    was perfectly good text -- just a byte per character. ASCII is tried
    first because a UTF-16 string reads as a single ASCII character followed
    by a terminator, which the length floor rejects, while the reverse
    mistake is not possible.
    """
    a = _asciiy(pine, addr, want)
    u = _texty(pine, addr, want)
    if a and u:
        # "You found 100 sandwiches!" in UTF-16 reads as the ASCII string "Y"
        # -- technically true and completely useless. Length breaks the tie.
        return ("ascii", a) if len(a) >= len(u) else ("utf16", u)
    if a:
        return "ascii", a
    if u:
        return "utf16", u
    return None, None


def _show_str(pine, addr):
    enc, s = _sniff(pine, addr)
    return f"  {s!r} ({enc})" if s else ""


_MSG_NAMES = {MSG_SHOW: "the show sequence number",
              MSG_UP: "the box-is-up flag",
              MSG_FLAG: "the flag beside the string slots"}
for _i, _a in enumerate(MSG_SLOTS):
    _MSG_NAMES[_a] = f"string slot {_i}"
for _i, _a in enumerate(MSG_CUR):
    _MSG_NAMES[_a] = f"currently-rendered string {_i}"


def _msg_annotate(pine, v, addr=None, budget=None):
    """What a changed word might mean, if it means anything."""
    known = _MSG_NAMES.get(addr)
    prefix = f"({known}) " if known else ""
    if budget is not None and budget[0] <= 0:
        return prefix.strip()
    if budget is not None:
        budget[0] -= 1
    enc, s = _sniff(pine, v, 3)
    if s is not None:
        return f"{prefix}POINTS AT TEXT ({enc}): {s!r}"
    if STR_TABLE <= v < STR_TABLE + STR_STRIDE * STR_COUNT:
        off = v - STR_TABLE
        if off % STR_STRIDE == 0:
            return f"{prefix}POINTS AT TABLE ENTRY {off // STR_STRIDE}"
        return f"{prefix}points inside table entry {off // STR_STRIDE}"
    if 0 < v < STR_COUNT:
        p = pine.read_u32(STR_TABLE + v * STR_STRIDE)
        s = _texty(pine, p)
        if s is not None:
            return f"{prefix}COULD BE INDEX {v}: {s!r}"
        return f"{prefix}could be index {v} (entry does not point at text)"
    if STR_COUNT <= v < STR_COUNT * 4:
        return (f"{prefix}index {v} would be PAST THE END -- "
                "this is an OOB STRING")
    if EE_MIN <= v < EE_MAX:
        return f"{prefix}points at 0x{v:08X} (not text)"
    return prefix.strip()


def _msg_capture(pine, windows):
    return {lo: read_block(pine, lo, hi - lo) for lo, hi in windows}


def _msg_diff(pine, quiet_a, quiet_b, live, windows):
    """Words that moved between quiet and live, minus per-frame churn.

    Two quiet snapshots are taken rather than one for exactly this reason: a
    running game rewrites hundreds of words every frame, and without a second
    reading there is no way to tell those apart from the handful the message
    box actually set.
    """
    out = []
    # Annotating dereferences memory, so a diff of thousands of words would
    # spend minutes describing noise. Past the budget the values still get
    # printed, just without a gloss.
    budget = [400]
    for lo, hi in windows:
        a, b, c = quiet_a[lo], quiet_b[lo], live[lo]
        n = min(len(a), len(b), len(c)) // 4
        for i in range(n):
            va = struct.unpack_from("<I", a, i * 4)[0]
            vb = struct.unpack_from("<I", b, i * 4)[0]
            if va != vb:
                continue                      # churn, not a message field
            vc = struct.unpack_from("<I", c, i * 4)[0]
            if vc == va:
                continue
            addr = lo + i * 4
            out.append((addr, va, vc, _msg_annotate(pine, vc, addr, budget)))
    return out


def _msg_rank(note):
    if not note:
        return 2
    # Keyword first, parenthetical second: a known slot that turned out to
    # hold text is the most interesting line on the page, not the least.
    if any(k in note for k in ("POINTS AT TEXT", "TABLE ENTRY",
                               "COULD BE INDEX", "PAST THE END")):
        return 0
    if note.startswith("(") and note.endswith(")"):
        return 3                              # a flag we already understand
    return 1


def _msg_report(pine, changes, title):
    print()
    print(f"    {title}: {len(changes)} word(s) changed and held still")
    print()
    # Anything the annotator recognised goes first: those are the candidates,
    # and the rest is background.
    ranked = sorted(changes, key=lambda c: (_msg_rank(c[3]), c[0]))
    for addr, was, now, note in ranked[:60]:
        print(f"      0x{addr:08X}  {was:>10} -> {now:<10}  "
              f"0x{now:08X}  {note}")
    if len(ranked) > 60:
        print(f"      ... and {len(ranked) - 60} more")


def _msg_windows(radius=0x2000):
    windows = []
    for lo, hi in sorted((a - radius, a + radius) for a in (MSG_SHOW, MSG_UP)):
        if windows and lo <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
        else:
            windows.append((lo, hi))
    return windows


def _msg_dismiss_watch(pine, args):
    """Watch a box being closed PROPERLY, by the player, with the controller.

    Writing 0 to the up flag hides a box but may not end the message as far
    as the game is concerned, which would explain the one symptom that has
    survived every fix: the first box appears and later ones do not. Rather
    than guess at more flags, this records what the game itself writes when a
    message really ends, so the teardown can be copied instead of invented.
    """
    windows = _msg_windows(int(args.radius, 0))
    print("    watching " + ", ".join(f"0x{lo:08X}-0x{hi:08X}"
                                      for lo, hi in windows))
    if not pine.read_u32(MSG_UP):
        print("    no box up -- raising one...")
        for a in MSG_SLOTS:
            pine.write_u32(a, int(args.via, 0))
        _msg_raise(pine)
        if not _msg_wait_up(pine, 2.0):
            print("    None appeared. If a box is already stuck open, dismiss")
            print("    it in game first and run this again.")
            return 1
    print("    box is up. Two quiet snapshots while it sits there...")
    quiet_a = _msg_capture(pine, windows)
    time.sleep(0.4)
    quiet_b = _msg_capture(pine, windows)
    print()
    print("    Now let it go away on its own -- it times out rather than")
    print("    waiting for a button, so nothing needs pressing.")
    deadline = time.time() + float(args.timeout)
    while time.time() < deadline:
        if not pine.read_u32(MSG_UP):
            break
        time.sleep(0.02)
    else:
        print("    Still up after the timeout. Nothing captured.")
        return 1
    time.sleep(0.15)
    live = _msg_capture(pine, windows)
    changes = _msg_diff(pine, quiet_a, quiet_b, live, windows)
    _msg_report(pine, changes, "a real dismissal")
    st = load_state()
    st["msg_dismiss"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "changes": changes}
    save_state(st)
    print()
    print("    Everything here except the up flag is teardown we have not")
    print("    been doing. Copying those writes is what should let a second")
    print("    box open.")
    return 0


# What the breakpoint turned the message box into: two linked lists.
#
# The instruction that hit was  sd a0,0x30(s0)  with s0 = 0x004C5CC0, three
# instructions after  ld a0,0x30(s0)  and  daddiu a0,a0,1 -- an increment of a
# 64-bit field. Beside it,  lw v1,0x20(s0) / lw a1,0x24(v1) / sw a1,0x20(s0)
# walks a node to its successor. So 0x004C5CF0 was never a flag: it is a
# COUNT, and 0x00509010 is the same field of a second list 0x14 lower down.
#
# That is why every flag theory failed. Writing 1 to a count says "there is
# one item", so the renderer draws whatever stale node the cursor happens to
# hold -- OOB STRING. Writing 0 says "the list is empty", so the box vanishes
# with no animation instead of ending. And an empty list points AT ITSELF:
# both containers read 0x004C5CC0 / 0x00508FE0 in their own pointer fields
# when nothing is queued, which is a circular sentinel and was sitting in
# plain sight in the very first dump.
MSG_LIST_B = 0x004C5CC0          # count at +0x30 is what was called MSG_UP
MSG_LIST_A = 0x00508FE0          # count at +0x30 is what was called MSG_SHOW
L_CURSOR, L_HEAD, L_ITER = 0x20, 0x24, 0x28
L_INDEX, L_COUNT = 0x2C, 0x30
NODE_NEXT, NODE_SIZE = 0x24, 0x38     # 0x38 is the allocator's a0 at the call


def cmd_msgnode(pine, args):
    """Walk the message lists node by node and dereference what they hold.

    The nodes are 0x38 bytes each, allocated one per message, and each one
    carries a pointer to the thing being shown. Following that pointer is the
    last hop -- everything up to here has been the plumbing.
    """
    bases = ([(int(args.container, 0), "?")] if args.container else
             [(MSG_LIST_B, "B"), (MSG_LIST_A, "A")])
    if args.wait:
        # A real message lasts a few seconds, which is not long enough to
        # start a command by hand and catch it. Wait for the count to move
        # and dump the instant it does.
        print(f"    waiting up to {args.wait}s for a message. Collect the "
              "sandwich now.")
        deadline = time.time() + float(args.wait)
        while time.time() < deadline:
            if any(pine.read_u32(b + L_COUNT) for b, _ in bases):
                print("    caught one.")
                break
            time.sleep(0.01)
        else:
            print("    Nothing queued before the timeout.")
            return 1
    print()
    for base, name in bases:
        cur = pine.read_u32(base + L_CURSOR)
        head = pine.read_u32(base + L_HEAD)
        cnt = pine.read_u32(base + L_COUNT)
        # The container carries its own name in the bytes before the pointer
        # fields -- list A reads "subtitle" -- which is worth printing rather
        # than only stumbling on when a node's next pointer happens to land
        # back on the sentinel.
        _, label = _sniff(pine, base, 3)
        print(f"    list {name} at 0x{base:08X}"
              + (f'   name: {label!r}' if label else ""))
        print(f"      cursor +0x20 = 0x{cur:08X}"
              + ("   (itself -- empty)" if cur == base else ""))
        print(f"      head   +0x24 = 0x{head:08X}"
              + ("   (itself -- empty)" if head == base else ""))
        print(f"      iter   +0x28 = 0x{pine.read_u32(base + L_ITER):08X}"
              f"   index +0x2C = {pine.read_u32(base + L_INDEX)}")
        print(f"      count  +0x30 = {cnt}")
        if head == base or cnt == 0:
            print("      nothing queued.")
            print()
            continue
        seen, node, n = set(), head, 0
        while node and node != base and node not in seen and n < int(args.max):
            seen.add(node)
            print()
            print(f"      node {n} at 0x{node:08X}")
            raw = read_block(pine, node, NODE_SIZE)
            for off in range(0, NODE_SIZE, 4):
                v = struct.unpack_from("<I", raw, off)[0]
                tag = "  <- next" if off == NODE_NEXT else ""
                note = _show_str(pine, v)
                if not note and EE_MIN <= v < EE_MAX:
                    note = "  points somewhere"
                print(f"        +0x{off:02X}  0x{v:08X}{note}{tag}")
            if args.deep or args.chase:
                for off in range(0, NODE_SIZE, 4):
                    v = struct.unpack_from("<I", raw, off)[0]
                    if EE_MIN <= v < EE_MAX and not (0x00640000 <= v < 0x00650000) \
                            and v != base:
                        if args.deep:
                            print()
                            print(f"      what +0x{off:02X} points at:")
                            _msg_inspect(pine, v, int(args.bytes, 0))
                        if args.chase:
                            # Chase from the value read THIS INSTANT. Copying
                            # an address out of one run into the next reads a
                            # slot the game has since reused, which is exactly
                            # how the last attempt found nothing but object
                            # names.
                            print()
                            print(f"      chasing 0x{v:08X} for text:")
                            found, n = _chase_from(pine, v, int(args.chase))
                            for path, a, enc, s in sorted(
                                    found, key=lambda f: (len(f[0]), f[0]))[:20]:
                                print(f"        {' -> '.join(path)}")
                                print(f"          = 0x{a:08X} ({enc}) {s!r}")
                            if not found:
                                print(f"        nothing readable in {n} objects")
                        break
            node = struct.unpack_from("<I", raw, NODE_NEXT)[0]
            n += 1
        print()
    print("    --deep also dumps whatever the node's value pointer holds,")
    print("    which is where the text has to be.")
    return 0


def _chase_from(pine, root, depth=3, width=0x80, budget=400, min_chars=3):
    """Breadth-first pointer walk. Returns [(path, addr, encoding, text)]."""
    seen, queue, found, visited = {root}, [(root, [])], [], 0
    while queue and visited < budget:
        addr, path = queue.pop(0)
        if len(path) >= depth:
            continue
        try:
            raw = read_block(pine, addr, width)
        except Exception:
            continue
        visited += 1
        for off in range(0, len(raw) - 3, 4):
            v = struct.unpack_from("<I", raw, off)[0]
            if not (EE_MIN <= v < EE_MAX - 0x80):
                continue
            step = f"0x{addr:08X}+0x{off:02X}"
            enc, s = _sniff(pine, v, min_chars)
            if s is not None:
                found.append((path + [step], v, enc, s))
            elif v not in seen:
                seen.add(v)
                queue.append((v, path + [step]))
    return found, visited


# The subtitle object holds a string INDEX, not a pointer.
#
# Nothing anywhere in RAM points at the message text except its own table
# entry -- which is why every hunt for a "live text pointer" came back with
# table entries and nothing else. The object at the end of the subtitle list
# carries the number instead, and the renderer looks it up. The proof is one
# line from an earlier dump: that object's +0x04 read 0x1A6 = 422, and
# table + 422*0x10 = 0x0069ECB0, which is exactly the "Congratulations!
# You've got 100 sandwiches" entry.
#
# So an out-of-range number is an OOB STRING, exactly as the game says, and
# there are two ways to change what a subtitle says: write a different index
# into the object, or repoint the entry that index names.
SUB_ID = 0x04


def _entry_addr(i):
    return STR_TABLE + i * STR_STRIDE


def _entry_text(pine, i):
    if not (0 <= i < STR_COUNT * 2):
        return None
    p = pine.read_u32(_entry_addr(i))
    return _texty(pine, p, 1)


def _live_subtitle(pine):
    """The object the subtitle list is currently holding, or None."""
    if not pine.read_u32(MSG_LIST_A + L_COUNT):
        return None
    node = pine.read_u32(MSG_LIST_A + L_HEAD)
    if not node or node == MSG_LIST_A:
        return None
    v = pine.read_u32(node + 0x20)
    return v if EE_MIN <= v < EE_MAX else None


def cmd_subtitle(pine, args):
    """Read, and change, the subtitle the game is showing.

    --id writes a different index into the live object, which changes the
    words for as long as that subtitle lasts and touches nothing permanent.

    --text goes the other way and repoints the table entry the subtitle names,
    which has no length limit because the entry is a pointer. The original is
    saved and put back when the subtitle ends.
    """
    if args.lookup:
        i = int(args.lookup, 0)
        print(f"    index {i}  entry 0x{_entry_addr(i):08X}  "
              f"-> {_entry_text(pine, i)!r}")
        return 0

    if args.wait:
        print(f"    waiting up to {args.wait}s for a subtitle...")
        deadline = time.time() + float(args.wait)
        while time.time() < deadline and not _live_subtitle(pine):
            time.sleep(0.01)
    obj = _live_subtitle(pine)
    if obj is None:
        print("    No subtitle queued. Use --wait, or --lookup N to read the")
        print("    table without one on screen.")
        return 1
    ident = pine.read_u32(obj + SUB_ID)
    print(f"    subtitle object 0x{obj:08X}, id +0x{SUB_ID:02X} = {ident}")
    print(f"    entry 0x{_entry_addr(ident):08X} -> "
          f"{_entry_text(pine, ident)!r}")

    if args.id:
        new = int(args.id, 0)
        print()
        print(f"    index {new} is {_entry_text(pine, new)!r}")
        pine.write_u32(obj + SUB_ID, new)
        print(f"    wrote it. The box should change now.")
        return 0

    if not args.text:
        print()
        print("    Show a different existing line:  subtitle --id 1339")
        print("    Show your own words:             subtitle --text \"...\"")
        return 0

    return _subtitle_retext(pine, args, ident)


def _subtitle_retext(pine, args, ident):
    """Repoint an entry, then wait for the subtitle that uses it.

    Order matters and I had it backwards. The renderer resolves the id ONCE,
    when the subtitle goes up -- which is why doing this by hand worked (the
    entry was already repointed before the box was raised) and why doing it
    after catching a live box changed nothing.
    """
    entry = _entry_addr(ident)
    orig = pine.read_u32(entry)
    orig_len = pine.read_u32(entry + 4)
    raw = args.text.encode("utf-16-le") + b"\0\0\0\0"
    dest = (int(args.scratch, 0) if args.scratch
            else _announce_scratch(pine, len(raw) + 16))
    if dest is None:
        return 1
    for i in range(0, len(raw), 4):
        pine.write_u32(dest + i, int.from_bytes(
            raw[i:i + 4].ljust(4, b"\0"), "little"))
    if not _wrote_ok(pine, dest, raw):
        print(f"    0x{dest:08X} would not hold the string.")
        return 1
    st = load_state()
    st["subtitle_entry"] = {"entry": entry, "ptr": orig, "len": orig_len}
    save_state(st)
    try:
        pine.write_u32(entry + 4, len(args.text))
        pine.write_u32(entry, dest)
        print(f"    entry 0x{entry:08X}: 0x{orig:08X} -> 0x{dest:08X}")
        print()
        print("    Now trigger the message. It is repointed BEFORE the box")
        print("    goes up, which is the part I had backwards.")
        end = time.time() + float(args.hold)
        while time.time() < end:
            time.sleep(0.05)
    finally:
        pine.write_u32(entry, orig)
        pine.write_u32(entry + 4, orig_len)
        st = load_state()
        st.pop("subtitle_entry", None)
        save_state(st)
        print(f"    restored -- index {ident} reads "
              f"{_entry_text(pine, ident)!r} again")
    return 0


VALUE_COPY = 0x100          # how much of the subtitle object to clone
HDR_SIZE   = 0x20           # how far back to look for an allocator header

# What the game itself writes when a message ends, caught in the msglife
# recording at t=7.147 for the subtitle list and t=7.162 for the overlay:
#
#   cursor -> the container itself      iter  -> 0
#   head   -> the container itself      index -> -2        count -> 0
#
# Note iter and index: the game writes literals, not "whatever was there
# before". Restoring a remembered iterator is how a cleared list can still
# have something to draw -- which is exactly what left a message stuck on
# screen after the first hand-built one.
EMPTY_ITER, EMPTY_INDEX = 0x00000000, 0xFFFFFFFE


def _list_empty(pine, base):
    """Empty a list the way the game does, in the order it does it."""
    pine.write_u32(base + L_CURSOR, base)
    pine.write_u32(base + L_HEAD, base)
    pine.write_u32(base + L_ITER, EMPTY_ITER)
    pine.write_u32(base + L_INDEX, EMPTY_INDEX)
    pine.write_u32(base + L_COUNT + 4, 0)
    pine.write_u32(base + L_COUNT, 0)


def _list_state(pine, base):
    return (pine.read_u32(base + L_CURSOR), pine.read_u32(base + L_HEAD),
            pine.read_u32(base + L_ITER), pine.read_u32(base + L_INDEX),
            pine.read_u32(base + L_COUNT))


FADE_PRESET_A = 0x004746A0      # chosen when object+0x0C & 2 is set
FADE_PRESET_B = 0x00474490      # chosen when it is clear
FADE_TARGET = 0x006466F0        # a1 to the fade call, s2/s3 at every break
PRESET_SIZE = 0x40


def cmd_subfade(pine, args):
    """Apply a fade preset by hand, instead of calling the function.

    The teardown's first act is z_un_0013cda0(preset, 0x006466F0, 1), and the
    two "names" turned out to be 64-byte structs that differ in one word --
    0x3C is 0 in one and 1 in the other. That reads like show and hide. If
    the call mostly copies the struct onto its target, doing the copy
    ourselves ends a message without ending anything: no erase, no free, no
    allocator involved.

    Which would make the whole crash problem go away. A fabricated subtitle
    that never gets torn down is fine if it can simply be hidden.
    """
    preset = int(args.preset, 0) if args.preset else FADE_PRESET_B
    target = int(args.target, 0) if args.target else FADE_TARGET
    n = int(args.bytes, 0)
    before = read_block(pine, target, n)
    src = read_block(pine, preset, n)
    print(f"    preset 0x{preset:08X} -> target 0x{target:08X}, {n} bytes")
    print()
    diff = [i for i in range(0, n, 4)
            if struct.unpack_from("<I", before, i)[0]
            != struct.unpack_from("<I", src, i)[0]]
    for i in diff[:16]:
        print(f"      +0x{i:02X}  0x{struct.unpack_from('<I', before, i)[0]:08X}"
              f" -> 0x{struct.unpack_from('<I', src, i)[0]:08X}")
    if not diff:
        print("      (identical already -- this preset is what is applied)")
    st = load_state()
    st["subfade_undo"] = {"target": target, "bytes": list(before)}
    save_state(st)
    if args.dry_run:
        print()
        print("    Dry run. Drop --dry-run to actually write it.")
        return 0
    _put(pine, target, src)
    print()
    print("    written. Look at the screen.")
    print("    Put it back with:  subfade --undo")
    return 0


def cmd_subundo(pine, args):
    st = load_state()
    u = st.get("subfade_undo")
    if not u:
        print("    Nothing saved to undo.")
        return 1
    _put(pine, u["target"], u["bytes"])
    st.pop("subfade_undo", None)
    save_state(st)
    print(f"    0x{u['target']:08X} restored ({len(u['bytes'])} bytes)")
    return 0


def cmd_subclear(pine, args):
    """Force both lists empty. For when something is stuck on screen."""
    for base, name in ((MSG_LIST_A, "subtitle"), (MSG_LIST_B, "overlay")):
        before = _list_state(pine, base)
        _list_empty(pine, base)
        after = _list_state(pine, base)
        print(f"    {name} 0x{base:08X}")
        print(f"      was  cursor 0x{before[0]:08X} head 0x{before[1]:08X} "
              f"iter 0x{before[2]:08X} index {before[3] - (1 << 32) if before[3] > 1 << 31 else before[3]} count {before[4]}")
        print(f"      now  cursor 0x{after[0]:08X} head 0x{after[1]:08X} "
              f"iter 0x{after[2]:08X} index -2 count {after[4]}")
    return 0


def cmd_sublearn(pine, args):
    """Photograph a real subtitle's node and object, to rebuild later.

    We cannot call the game's list-insert over PINE. What we CAN do is build
    a node and an object that look exactly like the ones it makes -- and the
    only honest way to know what those look like is to copy a real pair while
    one exists, rather than construct them from a layout guess.
    """
    print(f"    waiting up to {args.wait}s for a subtitle...")
    deadline = time.time() + float(args.wait)
    obj = None
    while time.time() < deadline:
        obj = _live_subtitle(pine)
        if obj:
            break
        time.sleep(0.01)
    if not obj:
        print("    None appeared.")
        return 1
    node = pine.read_u32(MSG_LIST_A + L_HEAD)
    node_bytes = read_block(pine, node, NODE_SIZE)
    obj_bytes = read_block(pine, obj, VALUE_COPY)
    # The bytes IN FRONT of each block. A debug allocator that takes a file
    # and a line number -- which this one does, a2 was 0x215 -- almost
    # certainly writes a header before what it hands back, and free() reads
    # it. A block with no header is what crashed the second teardown.
    node_hdr = read_block(pine, node - HDR_SIZE, HDR_SIZE)
    obj_hdr = read_block(pine, obj - HDR_SIZE, HDR_SIZE)
    print(f"    header in front of the node (0x{node - HDR_SIZE:08X}):")
    for off in range(0, HDR_SIZE, 4):
        v = struct.unpack_from("<I", node_hdr, off)[0]
        gap = node - HDR_SIZE + off
        print(f"      -0x{HDR_SIZE - off:02X}  0x{v:08X}"
              + ("   = 0x38, the size asked for" if v == NODE_SIZE else "")
              + _show_str(pine, v))
    ident = pine.read_u32(obj + SUB_ID)
    st = load_state()
    st["sub_template"] = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_at": node, "obj_at": obj, "id": ident,
        "node": list(node_bytes), "obj": list(obj_bytes),
        "node_hdr": list(node_hdr), "obj_hdr": list(obj_hdr),
        "empty": {"cursor": MSG_LIST_A, "head": MSG_LIST_A,
                  "iter": pine.read_u32(MSG_LIST_A + L_ITER),
                  "index": pine.read_u32(MSG_LIST_A + L_INDEX)},
    }
    save_state(st)
    print(f"    node 0x{node:08X} ({NODE_SIZE} bytes) and object "
          f"0x{obj:08X} ({VALUE_COPY} bytes) saved, id {ident}")
    print(f"    {_entry_text(pine, ident)!r}")
    print()
    print("    SAVE STATE, then try:  subraise 422")
    return 0


def _put(pine, addr, raw):
    for i in range(0, len(raw), 4):
        pine.write_u32(addr + i, int.from_bytes(
            bytes(raw[i:i + 4]).ljust(4, b"\0"), "little"))


def cmd_subraise(pine, args):
    """Build a subtitle by hand and hang it on the list.

    This is the one thing left that has never been tried, and it is genuinely
    risky: the game allocated every node it has ever seen, and when this
    message ends its teardown may try to free a node that came from our
    scratch buffer instead of its heap. That is a crash, not a glitch.

    So: SAVE STATE FIRST. Everything here is restored on the way out, and the
    list is put back to empty before the game gets a chance to tear anything
    down -- but "before" is a race, not a guarantee.
    """
    st = load_state()
    t = st.get("sub_template")
    if not t:
        print("    No template yet. Catch a real one first:")
        print("      sandwiches --set 99   then   sublearn --wait 60")
        return 1
    ident = int(args.id, 0) if args.id else t["id"]
    seconds = float(args.seconds)

    scratch = _announce_scratch(pine, VALUE_COPY + NODE_SIZE + 4 * HDR_SIZE,
                                0x400)
    if scratch is None:
        return 1
    obj_at = scratch + HDR_SIZE
    node_at = scratch + HDR_SIZE + VALUE_COPY + HDR_SIZE

    if pine.read_u32(MSG_LIST_A + L_COUNT):
        print("    A subtitle is already up. Let it finish first.")
        return 1

    # The objects the template points AT belong to the game, not to us. If
    # hanging our copy on the list makes the renderer write into one of them,
    # emptying the list afterwards will not undo it -- and something that
    # stays on screen after both lists are empty is being held exactly there.
    neighbours = []
    if args.watch:
        for i in range(0, len(t["obj"]) - 3, 4):
            v = int.from_bytes(bytes(t["obj"][i:i + 4]), "little")
            if EE_MIN <= v < EE_MAX - 0x100 and v not in neighbours:
                neighbours.append(v)
        neighbours = neighbours[:12]
        print(f"    watching {len(neighbours)} object(s) the template points at")
        watch_before = {a: read_block(pine, a, 0x100) for a in neighbours}

    if args.original:
        # Reuse the address the game's own object lived at, rather than a
        # copy in scratch. The renderer may already know that address -- and
        # if the visual teardown belongs to the object rather than the list,
        # a copy at a strange address is exactly what nothing knows how to
        # dispose of.
        obj_at = t["obj_at"]
        print(f"    reusing the game's own object address 0x{obj_at:08X}")

    print(f"    object -> 0x{obj_at:08X}, node -> 0x{node_at:08X}, id {ident}")
    print(f"    {_entry_text(pine, ident)!r}")
    _put(pine, obj_at, t["obj"])
    pine.write_u32(obj_at + SUB_ID, ident)
    _put(pine, node_at, t["node"])
    if args.header and t.get("node_hdr"):
        # The header is a heap block list: four of its eight words point at
        # the blocks either side. Copying them verbatim makes our block claim
        # to sit between two real ones, so free()'s unlink -- prev->next =
        # next, next->prev = prev -- rewrites THEIR links and splices out
        # whatever is really there. That survives once and kills the next
        # allocation, which is exactly the crash.
        #
        # Pointing them at ourselves instead makes the unlink write into our
        # own scratch and touch nothing the allocator owns.
        for blk, hdr, orig in ((obj_at, t["obj_hdr"], t["obj_at"]),
                               (node_at, t["node_hdr"], t["node_at"])):
            h = bytearray(bytes(hdr))
            base = blk - HDR_SIZE
            swapped = 0
            for off in range(0, HDR_SIZE, 4):
                v = int.from_bytes(h[off:off + 4], "little")
                if EE_MIN <= v < EE_MAX and abs(v - orig) < 0x10000:
                    struct.pack_into("<I", h, off, base)
                    swapped += 1
            _put(pine, base, bytes(h))
            print(f"    header at 0x{base:08X}: {swapped} neighbour link(s) "
                  "pointed back at itself")
    pine.write_u32(node_at + 0x20, obj_at)
    pine.write_u32(node_at + NODE_NEXT, MSG_LIST_A)      # next = sentinel
    try:
        pine.write_u32(MSG_LIST_A + L_HEAD, node_at)
        pine.write_u32(MSG_LIST_A + L_CURSOR, node_at)
        pine.write_u32(MSG_LIST_A + L_COUNT + 4, 0)
        pine.write_u32(MSG_LIST_A + L_COUNT, 1)
        print(f"    count 0 -> 1. Holding {seconds}s -- look at the screen.")
        time.sleep(seconds)
        if args.leave:
            # The one case never tried: don't yank it out, and see whether
            # the game's own end-message routine runs. Every failure so far
            # has been us removing the entry before anything could finish
            # with it, so "does it end itself" is still an open question.
            print()
            print("    LEAVING it on the list. If the game ends it by itself")
            print("    -- fade included -- then the teardown was never ours")
            print("    to do. If it crashes, its free() hit our scratch node.")
            print(f"    watch: count is {pine.read_u32(MSG_LIST_A + L_COUNT)}")
    finally:
        if args.leave:
            # Nothing to undo -- the game owns it now. (Returning from a
            # finally swallows exceptions and Python warns about it, which
            # is what that SyntaxWarning was.)
            pass
        else:
            _subraise_cleanup(pine, args, neighbours if args.watch else [],
                              watch_before if args.watch else {})
    return 0


def _subraise_cleanup(pine, args, neighbours, watch_before):
        # Empty it exactly the way the game does -- literals for iter and
        # index, not remembered values -- and before anything else can decide
        # to free our node.
        _list_empty(pine, MSG_LIST_A)
        if args.both:
            _list_empty(pine, MSG_LIST_B)
        c, h, it, ix, n = _list_state(pine, MSG_LIST_A)
        print(f"    emptied: cursor 0x{c:08X} head 0x{h:08X} "
              f"iter 0x{it:08X} index {ix - (1 << 32) if ix > 1 << 31 else ix} count {n}")
        if args.watch:
            time.sleep(0.4)
            print()
            changed = []
            for a in neighbours:
                after = read_block(pine, a, 0x100)
                b = watch_before[a]
                for i in range(0, min(len(b), len(after)) - 3, 4):
                    va = struct.unpack_from("<I", b, i)[0]
                    vb = struct.unpack_from("<I", after, i)[0]
                    if va != vb:
                        changed.append((a + i, va, vb))
                        print(f"      0x{a + i:08X}  0x{va:08X} -> "
                              f"0x{vb:08X}{_show_str(pine, vb)}")
            print()
            if not changed:
                print("    Nothing outside the list changed, so whatever is")
                print("    holding the picture is somewhere else again.")
            elif args.revert:
                # Only words that were a pointer BEFORE and are a pointer
                # AFTER. The first version reverted everything, and the
                # region turned out to be recycled memory full of timers and
                # floats -- 0x3A776464 is not a handle, it is a small float,
                # and writing it back puts stale garbage into a live object.
                safe = [(a, w, n) for a, w, n in changed
                        if EE_MIN <= w < EE_MAX and EE_MIN <= n < EE_MAX]
                skipped = len(changed) - len(safe)
                for addr, was, _ in safe:
                    pine.write_u32(addr, was)
                time.sleep(0.3)
                print(f"    put {len(safe)} pointer-shaped word(s) back"
                      + (f", skipped {skipped} that held numbers rather than"
                         " addresses" if skipped else ""))
                if skipped:
                    print("    (a word that was a float or a counter is not a")
                    print("    display handle, and restoring one is a way to")
                    print("    corrupt a live object rather than fix one)")
                print("    Look at the screen -- if the message is gone, that")
                print("    is the whole teardown.")
            else:
                print(f"    {len(changed)} word(s) in the game's own objects")
                print("    were changed by our raise and did NOT go back.")
                print("    Add --revert to put them back and see if the")
                print("    message clears.")


def cmd_textowner(pine, args):
    """Find the words on screen, then find what is POINTING at them.

    Chasing outward from an object failed twice, for the same reason both
    times: the object was gone or reused by the time it was read. Working
    inward does not have that problem. The string itself is static data and
    can be found whenever; the pointer to it only exists while the subtitle
    is up, and that pointer is the thing worth having -- rewrite it and the
    words change.

    So: read the message off the screen, type a few words of it here, and
    run this while the box is showing.
    """
    words = args.words
    tlo, thi = ((int(args.text_band[0], 0), int(args.text_band[1], 0))
                if args.text_band else (0x00600000, 0x00700000))
    plo, phi = ((int(args.band[0], 0), int(args.band[1], 0)) if args.band
                else (0x00400000, 0x00B00000))
    if args.full:
        tlo, thi = FULL_BAND
        plo, phi = FULL_BAND

    print(f"    looking for {words!r} in 0x{tlo:08X}-0x{thi:08X}")
    buf = read_block(pine, tlo, thi - tlo, progress=_bar)
    hits = []
    for label, pat in (("utf16", words.encode("utf-16-le")),
                       ("ascii", words.encode("ascii", "ignore"))):
        pos = 0
        while True:
            pos = buf.find(pat, pos)
            if pos < 0:
                break
            hits.append((tlo + pos, label))
            pos += 1
    if not hits:
        print("    Not there. Check the exact wording and capitals, or --full.")
        print("    A few distinctive words beat the whole sentence.")
        return 1
    # A pointer names the START of a string, and the phrase typed here is
    # usually somewhere in the middle of one. Walking back to the beginning
    # is the difference between finding the owner and finding nothing --
    # which is exactly what the first version of this did.
    def start_of(addr, enc):
        step = 2 if enc == "utf16" else 1
        best = addr
        while best - step >= tlo:
            off = best - step - tlo
            if enc == "utf16":
                if buf[off + 1] != 0 or not (0x20 <= buf[off] <= 0x7E):
                    break
            elif not (0x20 <= buf[off] <= 0x7E):
                break
            best -= step
        return best

    spans = []
    for a, enc in hits:
        s = start_of(a, enc)
        spans.append((s, a, enc))
    print(f"    {len(hits)} copy(ies):")
    for s, a, enc in spans[:12]:
        note = "" if s == a else f"  (phrase starts +0x{a - s:X} in)"
        print(f"      0x{s:08X}  ({enc})  {_sniff(pine, s, 1)[1]!r}{note}")

    if args.wait:
        print()
        print(f"    waiting up to {args.wait}s for a subtitle to be queued...")
        deadline = time.time() + float(args.wait)
        while time.time() < deadline:
            if pine.read_u32(MSG_LIST_A + L_COUNT):
                print("    caught one -- scanning now.")
                break
            time.sleep(0.01)
        else:
            print("    None queued. Scanning anyway, but a pointer that only")
            print("    exists while the box is up will not be there.")

    # A string can start mid-word, so pointers to any of the hits count --
    # and a pointer to the START of the containing string is what matters,
    # which is not necessarily where the search phrase begins.
    print()
    print(f"    scanning 0x{plo:08X}-0x{phi:08X} for pointers to them")
    pbuf = read_block(pine, plo, phi - plo, progress=_bar)
    # Every address from the start of the string up to the phrase counts: a
    # pointer into the middle of a string is unusual but a pointer to its
    # start is the normal case, and the phrase is rarely at the start.
    targets = set()
    for s, a, enc in spans:
        step = 2 if enc == "utf16" else 1
        targets.update(range(s, a + 1, step))
    owners = []
    for i, (v,) in enumerate(struct.iter_unpack("<I", pbuf)):
        if v in targets:
            owners.append((plo + i * 4, v))
    print()
    if not owners:
        print("    Nothing points at any copy. Either the box was not up, or")
        print("    the pointer is outside that band -- try --full.")
        return 1
    print(f"    {len(owners)} pointer(s):")
    for a, v in owners[:40]:
        _, s = _sniff(pine, v, 1)
        print(f"      0x{a:08X} -> 0x{v:08X}   {s!r}")
    st = load_state()
    st["textowner"] = {"words": words, "hits": hits, "owners": owners,
                       "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(st)
    print()
    print("    Rewrite one of those pointers while the box is up and the")
    print("    words should change:  poke 0xOWNER 0xYOUR_STRING")
    return 0


def cmd_msgchase(pine, args):
    """Follow pointers outward from an address until text turns up.

    The subtitle entry holds an object, that object holds more objects, and
    guessing which field is the text one hop at a time has cost a round trip
    every time. This walks the graph instead: read a block, treat every word
    that could be an address as one, and report the path to anything that
    reads as a string. Breadth first, so the shortest route wins, and capped
    so a bad root cannot wander off into all of RAM.
    """
    root = int(args.root, 0)
    width = int(args.width, 0)
    max_depth = int(args.depth)
    budget = int(args.budget)
    print(f"    chasing from 0x{root:08X}, {width} bytes per hop, "
          f"depth {max_depth}, up to {budget} objects")
    print()
    seen = {root}
    queue = [(root, [])]
    found, visited = [], 0
    while queue and visited < budget:
        addr, path = queue.pop(0)
        if len(path) >= max_depth:
            continue
        try:
            raw = read_block(pine, addr, width)
        except Exception:
            continue
        visited += 1
        for off in range(0, len(raw) - 3, 4):
            v = struct.unpack_from("<I", raw, off)[0]
            if not (EE_MIN <= v < EE_MAX - 0x80):
                continue
            enc, s = _sniff(pine, v, int(args.min_chars))
            step = f"0x{addr:08X}+0x{off:02X}"
            if s is not None:
                found.append((path + [step], v, enc, s))
                continue
            if v not in seen:
                seen.add(v)
                queue.append((v, path + [step]))
    print(f"    read {visited} object(s), found {len(found)} string(s)")
    print()
    # Shortest paths first: a string three hops out is more likely to be
    # incidental than one hanging directly off the object being examined.
    for path, v, enc, s in sorted(found, key=lambda f: (len(f[0]), f[0])):
        print(f"      {' -> '.join(path)}")
        print(f"        = 0x{v:08X}  ({enc})  {s!r}")
    if not found:
        print("    Nothing readable. Try --depth 4, or a wider --width, or a")
        print("    different root -- and if the message is not on screen while")
        print("    this runs, the objects it would reach do not exist yet.")
        return 1
    st = load_state()
    st["msgchase"] = {"root": root, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "found": [[p, v, e, s] for p, v, e, s in found]}
    save_state(st)
    return 0


def cmd_msglife(pine, args):
    """Record the message system in TIME ORDER, through a whole lifecycle.

    Before-and-after captures have taken this as far as they can. They found
    the words that differ, but not the order, and order is the whole question
    now: a box that blinks out instead of animating means the real ending is
    a sequence of writes we are not making, and a box whose text changes when
    Taz JUMPS means something upstream is re-resolving it while it sits there.

    So this watches a small, curated set of addresses fast enough to see
    single frames, and prints every change with a timestamp -- with the box
    going up and coming down marked, so the writes that belong to each are
    obvious. Run it, collect the hundredth sandwich, let the real message
    appear, dismiss it with the controller, and jump around while it is up.
    """
    radius = int(args.radius, 0)
    bands = []
    for anchor in (MSG_SLOTS[0], MSG_CUR[0]):
        bands.append((anchor - radius, anchor + radius))
    if args.band:
        bands = [(int(args.band[0], 0), int(args.band[1], 0))]
    total = sum(hi - lo for lo, hi in bands)
    seconds = float(args.seconds)
    print("    watching " + ", ".join(f"0x{lo:08X}-0x{hi:08X}"
                                      for lo, hi in bands)
          + f"  ({total} bytes) for {seconds:.0f}s")
    print()
    print("    Collect the sandwich, let the box appear, JUMP while it is up,")
    print("    and let it go away on its own -- it is timed, not dismissed.")
    print("    Whatever it writes on the way out is the teardown, and it is")
    print("    the game doing it rather than a button, which is better: it")
    print("    means the recording holds the real ending with nothing of")
    print("    ours mixed in.")
    print()

    def snap():
        return {lo: read_block(pine, lo, hi - lo) for lo, hi in bands}

    prev = snap()
    t0 = time.time()
    log, samples = [], 0
    while time.time() - t0 < seconds:
        cur = snap()
        samples += 1
        for lo, hi in bands:
            a, b = prev[lo], cur[lo]
            for i in range(min(len(a), len(b)) // 4):
                va = struct.unpack_from("<I", a, i * 4)[0]
                vb = struct.unpack_from("<I", b, i * 4)[0]
                if va != vb:
                    log.append((time.time() - t0, lo + i * 4, va, vb))
        prev = cur
    rate = samples / max(seconds, 0.001)
    print(f"    {samples} samples ({rate:.0f}/s), {len(log)} changes")
    if not log:
        print("    Nothing moved. Widen --radius, or the message never came.")
        return 1
    print()
    budget = [200]
    last_t = None
    for t, addr, was, now in log:
        if last_t is not None and t - last_t > 0.25:
            print("      ----")
        last_t = t
        mark = ""
        if addr == MSG_UP:
            mark = "   <<<< BOX UP" if now else "   <<<< BOX GONE"
        note = _msg_annotate(pine, now, addr, budget)
        print(f"      {t:7.3f}  0x{addr:08X}  {was:>10} -> {now:<10}"
              f"  {note}{mark}")
    st = load_state()
    st["msglife"] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "bands": bands, "log": log, "rate": rate}
    save_state(st)
    print()
    print("    The writes between BOX UP and BOX GONE are the message living.")
    print("    The ones AFTER BOX GONE are the teardown we have been skipping.")
    return 0


def cmd_msgwatch(pine, args):
    """Find the word that decides WHICH message the box shows.

    The box itself is solved -- 0x00509010 puts one up, 0x004C5CF0 takes it
    down -- but forcing it by hand shows "OOB STRING", meaning whatever selects
    the line is holding a number the game cannot look up. So: watch memory
    across a REAL message appearing, and the field the game set on the way is
    the selector.

    Run it twice.

      msgwatch                  stand at 99 sandwiches, run this, collect one
      msgwatch --manual         same capture, but triggered by writing SHOW=1
      msgwatch --compare        what the real one set that the fake one did not

    That last list is the answer, or contains it: a real message writes the
    selector, a forced one does not, and the difference is a very short list.
    """
    st = load_state()
    if args.compare:
        real = st.get("msg_real")
        fake = st.get("msg_manual")
        if not real or not fake:
            print("    Need both captures first:")
            print("      msgwatch            (then collect the 100th sandwich)")
            print("      msgwatch --manual")
            return 1
        fake_addrs = {c[0] for c in fake["changes"]}
        only = [c for c in real["changes"] if c[0] not in fake_addrs]
        print(f"    real capture:   {real['when']}, "
              f"{len(real['changes'])} changes")
        print(f"    forced capture: {fake['when']}, "
              f"{len(fake['changes'])} changes")
        _msg_report(pine, [tuple(c) for c in only],
                    "set by a real message and NOT by a forced one")
        print()
        print("    Read the annotated lines first. A word holding a small")
        print("    number, or a pointer into the table at "
              f"0x{STR_TABLE:08X},")
        print("    is the selector. Confirm it: force a box with `message`,")
        print("    write that word, and the text should change.")
        return 0

    if args.dismiss:
        return _msg_dismiss_watch(pine, args)

    radius = int(args.radius, 0)
    if args.band:
        windows = [(int(args.band[0], 0), int(args.band[1], 0))]
    else:
        # Sorted before merging. MSG_UP sits BELOW MSG_SHOW, so merging in the
        # order they are written swallows the lower window whole and watches
        # only half of what was asked for.
        windows = []
        for lo, hi in sorted((a - radius, a + radius)
                             for a in (MSG_SHOW, MSG_UP)):
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            else:
                windows.append((lo, hi))
    total = sum(hi - lo for lo, hi in windows)
    print("    watching " + ", ".join(f"0x{lo:08X}-0x{hi:08X}"
                                      for lo, hi in windows)
          + f"  ({total // 1024} KB)")

    if pine.read_u32(MSG_UP):
        print("    A message is up already. Dismissing it first.")
        pine.write_u32(MSG_UP, 0)
        time.sleep(0.5)

    print("    taking two quiet snapshots to learn what churns...")
    quiet_a = _msg_capture(pine, windows)
    time.sleep(0.4)
    quiet_b = _msg_capture(pine, windows)

    if args.manual:
        n, m = _msg_raise(pine)
        print(f"    forcing a box (sequence {n} -> {m})...")
        time.sleep(0.4)
        if not pine.read_u32(MSG_UP):
            print("    ...the game did not raise a box. Nothing to compare.")
            return 1
    else:
        print()
        print("    NOW go collect the sandwich. Waiting for a real message...")
        deadline = time.time() + float(args.timeout)
        while time.time() < deadline:
            if pine.read_u32(MSG_UP):
                break
            time.sleep(0.02)
        else:
            print("    Timed out with no message. Longer --timeout, or check")
            print("    the count is really 99 with `sandwiches`.")
            return 1
        print("    message is up -- capturing.")

    live = _msg_capture(pine, windows)
    changes = _msg_diff(pine, quiet_a, quiet_b, live, windows)
    _msg_report(pine, changes,
                "forced box" if args.manual else "real message")

    key = "msg_manual" if args.manual else "msg_real"
    st[key] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
               "windows": windows, "changes": changes}
    save_state(st)
    print()
    if args.manual:
        print("    Saved. If you have the real one too:  msgwatch --compare")
    else:
        print("    Saved. Now dismiss it (`message --hide`), then run")
        print("    `msgwatch --manual`, then `msgwatch --compare`.")
    return 0


def cmd_destruction(pine, args):
    """Both destruction numbers for every level, side by side.

    Verifies the parallel-array guess in one go rather than searching sixteen
    more levels: stand in a level, break something, and only that level's
    live figure should move.
    """
    here = pine.read_u32(LEVEL_ID)
    f = int(args.save_file)
    print(f"    save file {f}, currently in level {here}"
          f"  ({LEVEL_NAMES.get(here, '?')})")
    print()
    print(f"      {'lid':>3}  {'level':<21} {'live':>18}  {'best (saved)':>20}")
    for lid in range(FIRST_LEVEL, 21):
        la = live_destruction(lid, f)
        sa = level_block(lid, f) + L_DESTRUCTION
        lv, sv = pine.read_u32(la), pine.read_u32(sa)
        mark = "  <-- you are here" if lid == here else ""
        print(f"      {lid:>3}  {LEVEL_NAMES.get(lid, '?'):<21} "
              f"0x{la:08X}={lv:<4}  0x{sa:08X}={sv:<4}{mark}")
    print()
    print("    Break something and re-run. Only the level you are standing in")
    print("    should move, and only in the `live` column. If a live figure")
    print("    for a level you are not in moves, the array guess is wrong.")
    return 0


def cmd_verify(pine, args):
    """Are the addresses the same fight after fight?

    This is the question the whole pointer detour rests on. If the same
    absolute addresses still hold the scoreline after quitting to the hub and
    starting Gladiatoons again, they are static and there is no pointer to
    find -- which is what the other two bosses turned out to be.
    """
    st = load_state()
    if not st.get("taz"):
        print("    Nothing saved. Run `find` first.")
        return 1
    print("    Reload Gladiatoons (quit to the hub and re-enter) before")
    print("    running this, then play until the scoreline is not 0-0.")
    print()
    for who in ("taz", "daffy"):
        cands = {k: list(v) for k, v in st[who].items()}
        got = read_candidates(pine, cands)
        print(f"    {who}:")
        for name, vals in got.items():
            for a in sorted(vals):
                print(f"      {name:<6} 0x{a:08X} = {vals[a]}")
    print()
    print("    Compare those against the scoreline on screen.")
    print("    Still right  -> the addresses are STATIC. Use them directly;")
    print("                    no pointer, no chain, nothing to resolve.")
    print("    Now wrong    -> they are dynamic. Note the NEW addresses from")
    print("                    a fresh `find`, then run `ptrscan`.")
    return 0


def cmd_ptrscan(pine, args):
    """A pointer scan that survives a second opinion.

    One load is not evidence: any word that happens to sit a small distance
    below the target looks like a pointer to it. Run this once per load with
    that load's target address, and the intersection of the two runs is the
    only thing that can actually be a pointer to it.
    """
    target = int(args.target, 0)
    if args.band:
        lo, hi = int(args.band[0], 0), int(args.band[1], 0)
    else:
        lo, hi = (FULL_BAND if args.full else DEFAULT_BAND)
    maxoff = int(args.max_offset, 0)
    print(f"    target 0x{target:08X}, offsets up to 0x{maxoff:X}")
    print(f"    scanning 0x{lo:08X}-0x{hi:08X} for words pointing at it")
    buf = read_block(pine, lo, hi - lo, progress=_bar)

    hits = []
    floor = target - maxoff
    for i, (v,) in enumerate(struct.iter_unpack("<I", buf)):
        if floor <= v <= target and EE_MIN <= v < EE_MAX:
            hits.append((lo + i * 4, target - v))
    del buf
    print(f"    {len(hits)} words point into the target from within 0x{maxoff:X}")

    st = load_state()
    runs = st.get("ptrscan", [])
    # Scanning the same target twice is the same load looked at twice, not two
    # loads -- and intersecting a run with itself proves nothing. Replace.
    if runs and runs[-1]["target"] == target:
        print("    (same target as the last scan -- replacing it rather than")
        print("     counting one load as two)")
        runs[-1] = {"target": target, "hits": [[a, o] for a, o in hits]}
    else:
        runs.append({"target": target, "hits": [[a, o] for a, o in hits]})
    st["ptrscan"] = runs
    save_state(st)

    if len(runs) < 2:
        print()
        print("    That is one load. Reload Gladiatoons, find the score's NEW")
        print("    address, and run this again with that address -- a pointer")
        print("    has to hold up across both.")
        return 0

    common = None
    for r in runs:
        pairs = {(a, o) for a, o in r["hits"]}
        common = pairs if common is None else (common & pairs)
    print()
    print(f"    surviving {len(runs)} loads: {len(common)} pointer(s)")
    for a, o in sorted(common):
        print(f"      [0x{a:08X}] + 0x{o:X}")
    if not common:
        print("      None. Nothing at a fixed offset from a fixed word reaches")
        print("      the score, so it is not one hop away -- which usually")
        print("      means it lives inside an object reached from somewhere")
        print("      else entirely, or it was static all along and the two")
        print("      targets were simply the same address.")
    print()
    print("    Delete gladiatoons_probe.json's `ptrscan` list to start over.")
    return 0


def decide(samples, end, settle, verbose=None):
    """The proposed boss_lost rule, run over a list of samples.

    Kept separate from the reading so the same rule can be replayed over a
    recorded fight afterwards. A sample is a dict with t, clock, level, taz,
    daffy, state.

    The rule, and why each part is there:

      believed   The clock reads rubbish until the level has loaded, and the
                 rubbish is already above the limit -- so nothing counts
                 until it has been seen near zero.
      entry      The scores do NOT reset on a level load, so on entry they
                 still hold the LAST fight's result. Reading them before the
                 horn reports the previous fight.
      horn       The clock passing the limit. It keeps climbing afterwards,
                 so this is an edge, not a level.
      settle     Sudden death happens after the horn and platforms can still
                 change hands, so the pair has to stop moving before it is
                 read. This is the part that a single reading at the horn
                 gets wrong.
      once       One fight, one verdict.
    """
    believed = False
    horn_t = horn_clock = None
    entry_pair = None
    last_pair = None
    stable_since = None
    events = []
    for s in samples:
        t, clock, pair = s["t"], s["clock"], (s["taz"], s["daffy"])
        if s["level"] != GLAD_LEVEL:
            believed = False
            horn_t = entry_pair = last_pair = stable_since = None
            continue
        if clock < 1.0:
            if not believed:
                events.append((t, clock, "clock believed, fight starting"))
            believed = True
            horn_t = None
            entry_pair = pair
            stable_since = None
        if not believed:
            continue
        if last_pair is not None and pair != last_pair:
            events.append((t, clock, f"scores {last_pair[0]}-{last_pair[1]}"
                                     f" -> {pair[0]}-{pair[1]}"))
            stable_since = t
        last_pair = pair
        if horn_t is None and clock >= end:
            horn_t, horn_clock = t, clock
            stable_since = t
            events.append((t, clock, f"HORN -- scores read {pair[0]}-{pair[1]}"
                                     f", entry pair was "
                                     f"{entry_pair[0]}-{entry_pair[1]}"
                           if entry_pair else "HORN"))
            continue
        if horn_t is not None and stable_since is not None \
                and t - stable_since >= settle:
            verdict = {"t": t, "clock": clock, "horn_t": horn_t,
                       "horn_clock": horn_clock, "pair": pair,
                       "entry_pair": entry_pair,
                       "waited": round(t - horn_t, 2),
                       "lost": pair[1] > pair[0],
                       "tie": pair[0] == pair[1],
                       "changed_since_entry": entry_pair != pair}
            verdict["why"] = None
            return verdict, events
    if not believed:
        why = ("the clock was never seen near zero -- this was started after "
               "the fight had already begun, so nothing was trusted")
    elif horn_t is None:
        why = ("the clock never reached the horn -- the fight was left, "
               "restarted, or this was stopped too early")
    else:
        why = ("the horn was reached but the scores never held still for "
               "long enough before this stopped -- let it run longer after "
               "the result, or lower --settle")
    return None, events + [(None, None, "no verdict: " + why)]


def report(verdict, events, settle):
    for t, clock, what in events:
        if t is None:
            print(f"      {what}")
        else:
            print(f"      t={t:>7.2f}s  clock={clock:>8.2f}   {what}")
    print()
    if verdict is None:
        print("    NO VERDICT -- nothing would have been sent, which is the")
        print("    correct silence for every one of those cases.")
        return
    taz, daffy = verdict["pair"]
    print(f"    Verdict at t={verdict['t']:.2f}s, "
          f"{verdict['waited']:.2f}s after the horn "
          f"(settle was {settle:g}s)")
    print(f"      final scores   Taz {taz}  Daffy {daffy}")
    if verdict["entry_pair"]:
        e = verdict["entry_pair"]
        print(f"      pair on entry  {e[0]}-{e[1]}"
              + ("   <-- IDENTICAL to the result, so the change could not be"
                 if not verdict["changed_since_entry"] else "")
              )
        if not verdict["changed_since_entry"]:
            print("                     observed and the settle delay is the")
            print("                     only thing making this correct.")
    if verdict["tie"]:
        print("      TIE -- no DeathLink. Both start at 0 and steal from each")
        print("      other, so this is a real case, not a theoretical one.")
    elif verdict["lost"]:
        print("      >>> A DEATHLINK WOULD BE SENT. Daffy finished ahead.")
    else:
        print("      Taz won. No DeathLink, correctly.")


def cmd_deathlink(pine, args):
    """Watch a whole fight and say what boss_lost would have done.

    Reads only. Nothing is written to the game and nothing is sent anywhere,
    so this can be run against a real fight with no risk to a multiworld.
    Close the AP client anyway -- two PINE connections on one slot time each
    other out.
    """
    end, settle = float(args.end), float(args.settle)
    hz = max(2.0, float(args.hz))
    print(f"    horn at clock >= {end:g}, "
          f"scores must hold still for {settle:g}s after it")
    print(f"    Taz 0x{TAZ_SCORE:08X}   Daffy 0x{DAFFY_SCORE:08X}")
    print("    Reading only -- nothing is written and nothing is sent.")
    print("    Play the fight through the result. Ctrl-C when the screen")
    print("    after it has settled.")
    print()

    samples = []
    t0 = time.time()
    last_print = 0.0
    try:
        while True:
            t = round(time.time() - t0, 2)
            lid = pine.read_u32(LEVEL_ID)
            clock = pine.read_float(GLAD_TIMER)
            taz, daffy = pine.batch_u8([TAZ_SCORE, DAFFY_SCORE])
            st = "--"
            p = pine.read_u32(TAZ_PTR)
            if EE_MIN <= p < EE_MAX:
                so = pine.read_u32(p + O_STATE_PTR)
                if EE_MIN <= so < EE_MAX:
                    st = f"0x{pine.read_u8(so + S_STATE):02X}"
            samples.append({"t": t, "clock": round(clock, 3), "level": lid,
                            "taz": taz, "daffy": daffy, "state": st,
                            "f968": pine.read_u8(0x00380968),
                            "f980": pine.read_u8(0x00380980)})
            if t - last_print >= 0.5:
                last_print = t
                sys.stdout.write(
                    f"\r    t={t:7.2f}  clock={clock:8.2f}  lvl={lid:<3}"
                    f" state={st}  Taz={taz:<4}Daffy={daffy:<4}"
                    f" 968={samples[-1]['f968']:<4}980={samples[-1]['f980']:<4}")
                sys.stdout.flush()
            time.sleep(1.0 / hz)
    except KeyboardInterrupt:
        print("\n")

    verdict, events = decide(samples, end, settle)
    report(verdict, events, settle)

    st = load_state()
    st.setdefault("deathlink", []).append({
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end, "settle": settle, "expected": args.expected,
        "verdict": verdict, "events": events, "samples": samples})
    save_state(st)
    print()
    if args.expected != "unknown" and verdict is not None:
        got = "tie" if verdict["tie"] else ("loss" if verdict["lost"] else "win")
        ok = "MATCHES" if got == args.expected else "DOES NOT MATCH"
        print(f"    You said this fight was a {args.expected}; the rule says "
              f"{got}. {ok}.")
    print("    The recording is saved. `replay` re-runs the rule over it with")
    print("    different settings, so the settle delay can be tuned without")
    print("    fighting again.")
    return 0


def cmd_replay(pine, args):
    """Re-run the rule over recorded fights, at whatever settings.

    The point is the settle delay: too short and the verdict is read while
    sudden death is still swinging, too long and a quick restart is inside
    the window. Sweeping it over real recordings answers that without
    playing the fight again.
    """
    runs = load_state().get("deathlink", [])
    if not runs:
        print("    No recordings. Run `deathlink` through a fight first.")
        return 1
    settles = [float(x) for x in args.settle] if args.settle else \
        [0.0, 0.5, 1.0, 2.0, 3.0, 5.0]
    end = float(args.end)
    print(f"    {len(runs)} recording(s), horn at {end:g}\n")
    for i, r in enumerate(runs):
        exp = r.get("expected", "unknown")
        print(f"    recording {i + 1}  ({r['when']}, you called it {exp}, "
              f"{len(r['samples'])} samples)")
        for s in settles:
            v, _ = decide(r["samples"], end, s)
            if v is None:
                print(f"      settle {s:>4.1f}s   no verdict")
                continue
            got = "tie" if v["tie"] else ("loss" if v["lost"] else "win")
            flag = "" if exp == "unknown" else \
                ("  ok" if got == exp else "  <-- WRONG")
            print(f"      settle {s:>4.1f}s   {v['pair'][0]}-{v['pair'][1]}"
                  f"  {got:<5} {v['waited']:>5.2f}s after horn{flag}")
        print()
    print("    Pick the smallest settle that is right on every recording, and")
    print("    then add some margin -- a wrong DeathLink kills everyone else.")
    return 0


# Every boss counter, and what the client does with it. The two single-sided
# ones are tested with >= on every tick, so a value left over from a previous
# fight that already meets the threshold fires the moment the arena loads.
BOSS_COUNTERS = (
    (7,  "Elephant Pong", 0x0037D8FC, 4, 3),
    (12, "Gladiatoons Taz", 0x00380978, 1, None),
    (12, "Gladiatoons Daffy", 0x0038097C, 1, None),
    (19, "Disco Volcano", 0x00383EB0, 4, 6),
)


def cmd_bosses(pine, args):
    """Do the boss counters reset when the arena loads?

    Gladiatoons' pair does not -- it holds the previous fight's result until
    the next one finishes, which is why that one needs a settle delay. The
    other two are read as `counter >= lose_at` on every tick, with no horn
    and no settle, so if they are stale in the same way then walking into the
    arena after a fight that reached the threshold sends a DeathLink before
    the fight has even started.

    Run this, then walk into an arena, fight, leave, and walk back in.
    """
    hz = max(1.0, float(args.hz))
    print("    Walk into a boss arena, fight, leave, and walk back in.")
    print("    Ctrl-C to stop.")
    print()
    for lid, name, addr, size, lose_at in BOSS_COUNTERS:
        gate = f"fires at >= {lose_at}" if lose_at else "compared as a pair"
        print(f"      level {lid:<3} {name:<18} 0x{addr:08X}  "
              f"{'u8' if size == 1 else 'u32'}  {gate}")
    print()

    def snap():
        out = {}
        for lid, name, addr, size, _ in BOSS_COUNTERS:
            out[addr] = pine.read_u8(addr) if size == 1 else pine.read_u32(addr)
        return out

    log, prev_lid = [], None
    try:
        while True:
            lid = pine.read_u32(LEVEL_ID)
            vals = snap()
            if lid != prev_lid:
                log.append({"t": time.strftime("%H:%M:%S"), "level": lid,
                            "from": prev_lid, "vals": dict(vals)})
                print(f"\r    entered level {lid}"
                      + ("" if prev_lid is None else f" from {prev_lid}"))
                for l2, name, addr, _, lose_at in BOSS_COUNTERS:
                    v = vals[addr]
                    warn = ""
                    if l2 == lid and lose_at is not None and v >= lose_at:
                        warn = ("   <<< ALREADY AT OR PAST THE THRESHOLD -- "
                                "this would fire a DeathLink now")
                    elif l2 == lid and v != 0:
                        warn = "   <-- not zero on entry"
                    print(f"        {name:<18} = {v}{warn}")
                prev_lid = lid
            sys.stdout.write(
                f"\r    level={lid:<4} "
                + "  ".join(f"{n.split()[-1]}={vals[a]:<4}"
                            for _, n, a, _, _ in BOSS_COUNTERS))
            sys.stdout.flush()
            time.sleep(1.0 / hz)
    except KeyboardInterrupt:
        print("\n")

    print("    Level changes seen:")
    for e in log:
        print(f"      {e['t']}  -> level {e['level']}   "
              + "  ".join(f"{n.split()[-1]}={e['vals'][a]}"
                          for _, n, a, _, _ in BOSS_COUNTERS))
    print()
    print("    What to look for: the counter for the arena you just entered.")
    print("    Zero means it resets and the existing >= test is safe. Holding")
    print("    the last fight's number means those two bosses need the same")
    print("    treatment Gladiatoons got -- a horn and a settle, not a bare")
    print("    comparison on every tick.")
    st = load_state()
    st.setdefault("bosses", []).append(
        {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "log": log})
    save_state(st)
    return 0


SAVE_FIELDS = [
    (0x000, "L_COMPLETE      level/boss complete", 4),
    (0x1E4, "L_SANDWICHES", 4),
    (0x210, "L_POSTERS_DONE", 4),
    (0x214, "L_BOUNTY_DEDUCT", 4),
    (0x218, "L_TOTAL_BOUNTY", 4),
    (0x21C, "L_DESTRUCTION   best ever", 4),
    (0x224, "L_ACCESS        <- the client forces this", 4),
    (0x228, "L_GOLDEN_SAM", 4),
    (0x230, "L_BONUS_GAME", 4),
    (0x234, "L_SECONDS", 4),
]


HAUNTED_LEVEL = 14
SPIN_REQUEST = 0x0C


# On-screen text is UTF-16LE in packed tables. The known ones sit between
# 0x006A0000 and 0x006C0000; a string being DISPLAYED may well be somewhere
# else entirely, which is the point of searching rather than assuming.
TEXT_BAND = (0x006A0000, 0x006C0000)


def _read_w(pine, addr, max_chars=120):
    raw = read_block(pine, addr, max_chars * 2)
    out = []
    for i in range(0, len(raw) - 1, 2):
        cp = raw[i] | (raw[i + 1] << 8)
        if cp == 0:
            break
        if cp < 0x20 or cp > 0x7E:
            out.append(".")
        else:
            out.append(chr(cp))
    return "".join(out)


def cmd_sequence(pine, args):
    """Sweep a band as fast as possible and report changes in TIME ORDER.

    A before-and-after capture throws away the one thing that separates a
    cause from its consequences: which happened first. This keeps the order,
    groups changes into waves separated by a quiet gap, and prints the
    earliest wave first -- that wave is where a trigger lives, and everything
    after it is the game reacting.

    Narrow the band hard. The sample rate is the whole point, and it is
    inversely proportional to how much is being read; the achieved rate is
    printed so you know what resolution you actually got.
    """
    lo, hi = int(args.band[0], 0), int(args.band[1], 0)
    gap = float(args.gap)
    print(f"    band 0x{lo:08X}-0x{hi:08X} ({(hi - lo) / 1024:.0f} KB)")
    print("    Sampling as fast as it can. Make the thing happen, then Ctrl-C.")
    print()
    base = None
    events = []
    samples = 0
    t0 = time.time()
    last_report = 0.0
    try:
        while True:
            buf = read_block(pine, lo, hi - lo)
            t = time.time() - t0
            samples += 1
            if base is None:
                base = bytearray(buf)
            else:
                raw = {}
                diff_into(raw, lo, base, buf, round(t, 3))
                for addr, evs in raw.items():
                    for ev in evs:
                        events.append((ev[0], addr, ev[1], ev[2]))
            if t - last_report >= 1.0:
                last_report = t
                sys.stdout.write(
                    f"\r    {t:6.1f}s  {samples} samples "
                    f"({samples / max(t, 0.001):.1f}/s)  "
                    f"{len(events)} change(s)   ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n")

    rate = samples / max(time.time() - t0, 0.001)
    print(f"    {samples} samples at {rate:.1f}/s "
          f"-- about {1000 / max(rate, 0.001):.0f} ms between reads.")
    if not events:
        print("    Nothing changed. Wrong band, or nothing happened.")
        return 0

    events.sort()
    waves, cur = [], [events[0]]
    for e in events[1:]:
        if e[0] - cur[-1][0] > gap:
            waves.append(cur)
            cur = [e]
        else:
            cur.append(e)
    waves.append(cur)
    print(f"    {len(events)} changes in {len(waves)} wave(s), "
          f"split on gaps over {gap:g}s.\n")
    for i, w in enumerate(waves[:int(args.waves)]):
        head = "  <-- EARLIEST: a cause lives here" if i == 0 else ""
        print(f"    wave {i + 1}: t={w[0][0]:.3f}s, {len(w)} change(s){head}")
        by_addr = {}
        for t, addr, b, a in w:
            by_addr.setdefault(addr, []).append((t, b, a))
        for addr in sorted(by_addr)[:20]:
            seq = by_addr[addr]
            vals = " ".join(str(a) for _, _, a in seq[:8])
            print(f"      0x{addr:08X}  first t={seq[0][0]:.3f}  "
                  f"{seq[0][1]} -> {vals}")
        if len(by_addr) > 20:
            print(f"      ... and {len(by_addr) - 20} more addresses")
        print()
    st = load_state()
    st.setdefault("sequence", []).append(
        {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "band": [lo, hi],
         "rate": round(rate, 2),
         "waves": [[[t, a, b, c] for t, a, b, c in w[:60]]
                   for w in waves[:6]]})
    save_state(st)
    return 0


def cmd_capture(pine, args):
    """Record what changes when something happens, then do it again.

    Snapshot, you make the thing happen, snapshot again -- the difference is
    what the game did. `--replay` writes those values back, which is the test
    that matters: if replaying them makes the message box appear a second
    time, that IS the trigger, and the client can do exactly the same.

    Narrow the band until the difference is small. A wide band during play
    catches every counter and animation in the game as well as the thing you
    care about.
    """
    st = load_state()
    key = f"capture_{args.name}"

    def shortlist(diffs):
        """The words that look like a cause rather than a consequence.

        A flag going up, a flag going down, or a small number changing to
        another small number. Everything else in a capture taken during play
        is animation, physics and render state.
        """
        out = {}
        for a, (b, c) in diffs.items():
            if (b == 0 and 0 < c <= 4) or (c == 0 and 0 < b <= 4) \
                    or (max(b, c) <= 32 and b != c):
                out[a] = (b, c)
        return out

    if args.replay:
        rec = st.get(key)
        if not rec:
            print(f"    Nothing recorded under {args.name!r}. Capture first.")
            return 1
        diffs = {int(a): tuple(v) for a, v in rec["diffs"].items()}
        if rec.get("common"):
            diffs = {int(a): tuple(v) for a, v in rec["common"].items()}
            print(f"    using the {len(diffs)} word(s) that changed on EVERY "
                  f"capture")
        if args.interesting:
            diffs = shortlist(diffs)
            print(f"    narrowed to {len(diffs)} flag-shaped word(s)")
        if args.only:
            want = {int(x, 0) for x in args.only}
            diffs = {a: v for a, v in diffs.items() if a in want}
            print(f"    narrowed to the {len(diffs)} you named")
        pairs = [(a, b, c) for a, (b, c) in diffs.items()]
        cap = int(args.max)
        print(f"    {rec['name']}: {len(pairs)} word(s) recorded "
              f"({rec['when']})")
        if len(pairs) > cap:
            print(f"    REFUSED: more than --max {cap}. Narrow the band and")
            print("    capture again -- replaying hundreds of words at once")
            print("    is how a game gets corrupted rather than triggered.")
            return 1
        for addr, before, after in sorted(pairs):
            pine.write_u32(addr, after)
            print(f"      0x{addr:08X}  {before} -> {after}")
        print()
        print("    Written. If the box appeared, that is the trigger.")
        return 0

    lo, hi = int(args.band[0], 0), int(args.band[1], 0)
    print(f"    band 0x{lo:08X}-0x{hi:08X} ({(hi - lo) / 1024:.0f} KB), "
          f"recording as {args.name!r}")
    print("    Taking the BEFORE snapshot...")
    before = read_block(pine, lo, hi - lo, progress=_bar)
    print()
    try:
        input("    Now make the thing happen, then press Enter > ")
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    after = read_block(pine, lo, hi - lo, progress=_bar)

    diffs = {}
    for i in range(0, min(len(before), len(after)) - 3, 4):
        b = int.from_bytes(before[i:i + 4], "little")
        a = int.from_bytes(after[i:i + 4], "little")
        if a != b:
            diffs[lo + i] = (b, a)
    print()
    print(f"    {len(diffs)} word(s) changed.")
    for addr, (b, a) in list(sorted(diffs.items()))[:40]:
        print(f"      0x{addr:08X}  {b:>10} -> {a:<10}  "
              f"0x{b:08X} -> 0x{a:08X}")
    if len(diffs) > 40:
        print(f"      ... and {len(diffs) - 40} more")

    prev = st.get(key)
    common = None
    if prev and not args.fresh:
        old = {int(a): tuple(v) for a, v in prev["diffs"].items()}
        # A word that changed BOTH times, to the same value both times, is
        # part of the thing itself. Anything that changed once is scenery.
        common = {a: v for a, v in diffs.items()
                  if a in old and old[a][1] == v[1]}
        print()
        print(f"    against the previous capture: {len(common)} word(s) "
              f"changed the same way both times")
        for a, (b, c) in sorted(common.items())[:30]:
            print(f"      0x{a:08X}  {b} -> {c}")

    st[key] = {"name": args.name, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
               "band": [lo, hi],
               "diffs": {str(k): list(v) for k, v in diffs.items()},
               "common": ({str(k): list(v) for k, v in common.items()}
                          if common else None)}
    save_state(st)

    short = shortlist(common if common else diffs)
    print()
    print(f"    {len(short)} of them are flag-shaped -- a cause rather than a")
    print("    consequence:")
    for a, (b, c) in sorted(short.items())[:20]:
        print(f"      0x{a:08X}  {b} -> {c}")
    print()
    print("    Do it a second time to halve the noise. Then:")
    print(f"      capture --name {args.name} --replay --interesting")
    if len(diffs) > int(args.max):
        print(f"    That is more than --max {args.max}, so narrow the band")
        print("    first -- most of those will be counters and animation.")
    return 0


def cmd_strtable(pine, args):
    """Find the table of POINTERS to strings, and read it as an index space.

    A game that says "OOB STRING" is looking a string up by NUMBER, which
    means there is an array of pointers somewhere and the number is an index
    into it. Given the address of any string, this finds the word that points
    at it, walks outward while the neighbours also look like string pointers,
    and prints the whole table with its indices.

    None of it needs the string to be on screen: a pointer table is static.
    """
    target = int(args.target, 0)
    lo, hi = ((int(args.band[0], 0), int(args.band[1], 0)) if args.band
              else FULL_BAND)
    tlo, thi = int(args.text_lo, 0), int(args.text_hi, 0)
    print(f"    looking for a word holding 0x{target:08X}, "
          f"in 0x{lo:08X}-0x{hi:08X}")
    buf = read_block(pine, lo, hi - lo, progress=_bar)

    hits = [lo + i * 4 for i, (v,) in enumerate(struct.iter_unpack("<I", buf))
            if v == target]
    print(f"    {len(hits)} word(s) point at it: "
          + ", ".join(f"0x{a:08X}" for a in hits[:8]))
    if not hits:
        print("      None. Either the band is wrong -- try --full -- or the")
        print("      game reaches its strings some other way than an array")
        print("      of pointers.")
        return 0

    def word(a):
        if not (lo <= a < hi - 3):
            return None
        return struct.unpack_from("<I", buf, a - lo)[0]

    def is_text_ptr(a):
        v = word(a)
        return v is not None and tlo <= v < thi

    for hit in hits[:int(args.tables)]:
        # Show the raw neighbourhood first. If the entries are structs rather
        # than bare pointers, the shape is obvious here and nowhere else.
        print()
        print(f"    around 0x{hit:08X}, one word per line:")
        for a in range(hit - 0x20, hit + 0x24, 4):
            v = word(a)
            if v is None:
                continue
            tag = ""
            if tlo <= v < thi:
                tag = f"  -> {_read_w(pine, v, 28)!r}"
            here = "  <<<" if a == hit else ""
            print(f"      0x{a:08X}  {v:>10}  0x{v:08X}{tag}{here}")

        # An entry may be a struct, so the stride is not necessarily 4. Try a
        # few and keep whichever produces the longest run of text pointers.
        # Every stride is reported, because the longest run is not always the
        # right answer: an array of 16-byte structs holding two pointers reads
        # as a longer run at stride 8, alternating between two different
        # fields. The tell is that the entries then point into two different
        # regions instead of one, so strides are ranked by how HOMOGENEOUS
        # their targets are first and by length second.
        runs = []
        for s in (4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 64):
            first_s = hit
            while is_text_ptr(first_s - s):
                first_s -= s
            last_s = hit
            while is_text_ptr(last_s + s):
                last_s += s
            n = (last_s - first_s) // s + 1
            if n < 2:
                continue
            pages = {(word(first_s + i * s) or 0) >> 16 for i in range(n)}
            runs.append((len(pages), -n, s, first_s, n))
        if not runs:
            print()
            print("    No run at any stride from 4 to 64 -- this looks like a")
            print("    single reference rather than a table. Read the word")
            print("    dump above: whatever sits beside it is what selects it.")
            continue
        runs.sort()
        print()
        print("    strides that produce a run:")
        for pages, negn, s, f_s, n in runs:
            print(f"      stride 0x{s:<3X} {n:>6} entries, targets span "
                  f"{pages} region(s){'   <- best fit' if (pages, negn, s, f_s, n) == runs[0] else ''}")
        if args.stride:
            stride = int(args.stride, 0)
            first = hit
            while is_text_ptr(first - stride):
                first -= stride
            last = hit
            while is_text_ptr(last + stride):
                last += stride
            count = (last - first) // stride + 1
        else:
            _, _, stride, first, count = runs[0]
        index = (hit - first) // stride
        print()
        if count <= 1:
            print(f"    No run found at any stride from 4 to 64 -- this looks")
            print(f"    like a single reference rather than a table. The word")
            print(f"    dump above is the thing to read: whatever sits beside")
            print(f"    0x{hit:08X} is what selects it.")
            continue
        print(f"    table at 0x{first:08X}, stride 0x{stride:X}, "
              f"{count} entries, 0x{target:08X} is index {index}")
        show = int(args.show)
        for i in range(max(0, index - show), min(count, index + show + 1)):
            a = first + i * stride
            p = word(a)
            s = _read_w(pine, p, 44) if p is not None else ""
            mark = "  <-- the one you gave me" if a == hit else ""
            print(f"      [{i:>4}] 0x{a:08X} -> 0x{p:08X}  {s!r}{mark}")
        print(f"    indices run 0 .. {count - 1}; anything past that is what")
        print("    an OOB STRING is complaining about.")
    return 0


def cmd_findtext(pine, args):
    """Search RAM for a string, as UTF-16LE and as plain ASCII.

    For chasing what the game is DISPLAYING rather than what it has stored:
    read a line off the screen, find every copy of it, and the one that is
    not in the known text tables is the live one.
    """
    text = args.text
    lo, hi = (FULL_BAND if args.full else
              (TEXT_BAND if args.text_band else DEFAULT_BAND))
    print(f"    searching 0x{lo:08X}-0x{hi:08X} for {text!r}")
    buf = read_block(pine, lo, hi - lo, progress=_bar)
    for label, pat in (("utf-16le", text.encode("utf-16-le")),
                       ("ascii", text.encode("ascii", "ignore"))):
        hits, pos = [], 0
        while True:
            pos = buf.find(pat, pos)
            if pos < 0:
                break
            hits.append(lo + pos)
            pos += 1
        print(f"      {label:<9} {len(hits)} hit(s)")
        for a in hits[:20]:
            known = ""
            if 0x006A0000 <= a < 0x006C0000:
                known = "   (inside the known text tables)"
            print(f"        0x{a:08X}{known}")
        if len(hits) > 20:
            print(f"        ... and {len(hits) - 20} more")
    print()
    print("    A hit OUTSIDE the tables is the interesting one -- that is a")
    print("    copy the game made to show it, and whatever points at it is")
    print("    what a custom message would have to be fed through.")
    return 0


def cmd_readtext(pine, args):
    """Read a UTF-16LE string, following a chain if given one."""
    a = resolve_addr(pine, args.address)
    if a is None:
        print(f"    {args.address} does not resolve.")
        return 1
    print(f"    {args.address} -> 0x{a:08X}")
    print(f"      {_read_w(pine, a, int(args.chars))!r}")
    return 0


def cmd_writetext(pine, args):
    """Write a UTF-16LE string in place, refusing to overrun the slot.

    The tables are PACKED, so a string longer than the one already there eats
    the next entry. Capacity defaults to the length of what is currently in
    the slot, which is the only safe ceiling.
    """
    a = resolve_addr(pine, args.address)
    if a is None:
        print(f"    {args.address} does not resolve.")
        return 1
    cur = _read_w(pine, a)
    cap = int(args.capacity) if args.capacity else len(cur)
    print(f"    0x{a:08X} currently holds {cur!r} ({len(cur)} chars)")
    if len(args.text) > cap:
        print(f"    REFUSED: {args.text!r} is {len(args.text)} chars, "
              f"capacity {cap}.")
        print("    Pass --capacity if you have measured the real slot size.")
        return 1
    raw = args.text.encode("utf-16-le")
    span = (cap + 1) * 2
    payload = raw + b"\0" * (span - len(raw))
    for i in range(0, len(payload), 4):
        chunk = payload[i:i + 4].ljust(4, b"\0")
        pine.write_u32(a + i, int.from_bytes(chunk, "little"))
    print(f"    now holds {_read_w(pine, a)!r}")
    return 0


def cmd_unball(pine, args):
    """Rehearse the ball recovery exactly as the client will do it.

    The AP client and this cannot both hold PINE slot 28011, so the client
    has to be CLOSED for this -- which is the point: it runs the same rule
    and the same write, so a run here proves the mechanism on the real game
    before trusting it in a session.

    This WRITES: one spin request when it sees the ball die, re-asserted
    while Taz is still the ball or still nothing, and stopped the moment he
    is anything else.
    """
    hz = max(2.0, float(args.hz))
    hold = float(args.hold)
    print(f"    Watching level {HAUNTED_LEVEL} for a death as the mouse or "
          f"the ball.")
    print(f"    On one, it asks for a spin (request <- 0x{SPIN_REQUEST:02X}) "
          f"for up to {hold:g}s.")
    print("    This writes. Close the AP client first. Ctrl-C to stop.")
    print()
    prev, until, said, fired = None, 0.0, False, []
    t0 = time.time()
    try:
        while True:
            t = round(time.time() - t0, 2)
            lid = pine.read_u32(LEVEL_ID)
            loading = pine.read_u32(GAME_STATE) != 1
            st = None
            p = pine.read_u32(TAZ_PTR)
            if EE_MIN <= p < EE_MAX:
                so = pine.read_u32(p + O_STATE_PTR)
                if EE_MIN <= so < EE_MAX:
                    st = pine.read_u8(so + S_STATE)
            ra = resolve_addr(pine, f"[[{TAZ_PTR:#x}]+{O_STATE_PTR:#x}]"
                                    f"+{S_REQUEST:#x}")

            if (lid == HAUNTED_LEVEL and prev in TRANSFORM_STATES
                    and st == 0x00 and not loading and not until):
                until, said = time.time() + hold, False
                print(f"\r    t={t:>7.2f}  DIED AS "
                      f"{state_name(prev)} -> asking for a spin"
                      f"{' ' * 20}")
                fired.append(t)

            if until:
                if time.time() > until:
                    print(f"\r    t={t:>7.2f}  gave up after {hold:g}s -- "
                          f"state is {state_name(st)}{' ' * 20}")
                    until = 0.0
                elif st is not None and st not in (0x00, 0x51, 0x52):
                    print(f"\r    t={t:>7.2f}  it took -- Taz is now "
                          f"{state_name(st)}{' ' * 25}")
                    until = 0.0
                elif ra is not None:
                    pine.write_u32(ra, SPIN_REQUEST)
                    said = True

            if st != prev and st is not None:
                prev = st
            sys.stdout.write(
                f"\r    t={t:>7.2f}  lvl={lid:<4} {state_name(st):<30} "
                f"request={'--' if ra is None else f'0x{pine.read_u32(ra):02X}'}"
                f" {'RECOVERING' if until else '          '}  ")
            sys.stdout.flush()
            time.sleep(1.0 / hz)
    except KeyboardInterrupt:
        print("\n")
    print(f"    {len(fired)} ball death(s) seen"
          + (f" at t={', '.join(f'{x:.2f}' for x in fired)}" if fired else ""))
    if not fired:
        print("      None seen. If you did die as the ball, run `death` and")
        print("      check what state it went to -- the rule wants 0x00.")
    return 0


def cmd_saveblock(pine, args):
    """Dump a level's save block, and diff it against the last dump.

    For finding what the client changed: capture with it closed, capture
    again with it connected, and the difference is everything it wrote. The
    access field at +0x224 is the one to watch -- the client forces it to
    0x20 for an unlocked level and 0x21 for a hub, and if the game keeps
    anything else in that field those bits are being flattened.
    """
    lid = int(args.level)
    f = int(args.save_file)
    base = level_block(lid, f)
    label = args.label or "capture"
    print(f"    level {lid} ({LEVEL_NAMES.get(lid, '?')}), save file {f}")
    print(f"    block 0x{base:08X}, labelled {label!r}")
    print()

    now = {}
    for off, name, size in SAVE_FIELDS:
        now[off] = pine.read_u32(base + off)
        print(f"      +0x{off:03X}  {name:<42} = {now[off]}"
              f"  (0x{now[off]:X})")

    raw = read_block(pine, base, 0x238)
    st = load_state()
    key = f"saveblock_{lid}_{f}"
    prev = st.get(key)
    st[key] = {"label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
               "fields": {str(k): v for k, v in now.items()},
               "raw": raw.hex()}
    save_state(st)

    if prev:
        print()
        print(f"    against the previous dump ({prev['label']}, "
              f"{prev['when']}):")
        old = bytes.fromhex(prev["raw"])
        diffs = [i for i in range(min(len(old), len(raw))) if old[i] != raw[i]]
        if not diffs:
            print("      identical -- the client changed nothing in this block")
        else:
            words = sorted({i & ~3 for i in diffs})
            for w in words[:40]:
                o = int.from_bytes(old[w:w + 4], "little")
                n = int.from_bytes(raw[w:w + 4], "little")
                named = next((nm for off, nm, _ in SAVE_FIELDS if off == w), "")
                print(f"      +0x{w:03X}  {o} -> {n}"
                      f"   (0x{o:X} -> 0x{n:X})  {named}")
            if len(words) > 40:
                print(f"      ... and {len(words) - 40} more words")
    else:
        print()
        print("    First dump for this level. Change one thing -- connect or")
        print("    close the client -- reload the level, and run it again with")
        print("    a different --label to see exactly what moved.")
    return 0


def cmd_death(pine, args):
    """Dry-run the ordinary-death rule, naming every state as it happens.

    Reads only. Nothing is written and nothing is sent, so this can be run
    against a real death with no risk to a multiworld -- close the AP client
    first so the two are not both on PINE.

    It mirrors death_tick: nothing counts until Taz has been seen ALIVE (a
    state that is not a death and not the zero a half-built object reads),
    the report is the transition INTO a death rather than sitting in one, and
    the void inside a boss arena is a phase change rather than a death.
    """
    hz = max(2.0, float(args.hz))
    print("    Reading only. Play until you die; Ctrl-C after.")
    print("    Every state change is listed, so a transformation that does")
    print("    something unexpected on death shows up whether or not it")
    print("    counts as a death.")
    print()
    seq, armed, prev, last_level = [], False, None, None
    fired = []
    t0 = time.time()
    try:
        while True:
            t = round(time.time() - t0, 2)
            lid = pine.read_u32(LEVEL_ID)
            loading = pine.read_u32(GAME_STATE) != 1
            st = None
            p = pine.read_u32(TAZ_PTR)
            if EE_MIN <= p < EE_MAX:
                so = pine.read_u32(p + O_STATE_PTR)
                if EE_MIN <= so < EE_MAX:
                    st = pine.read_u8(so + S_STATE)
            cos = None
            ca = resolve_addr(pine, f"[[{TAZ_PTR:#x}]+{O_COSTUME_PTR:#x}]"
                                    f"+{C_COSTUME:#x}")
            if ca is not None:
                cos = pine.read_u8(ca)

            if lid != last_level:
                print(f"\r    t={t:>7.2f}  entered level {lid}"
                      f"{' ' * 40}")
                last_level, armed, prev = lid, False, None

            if prev is None:
                if not loading and st is not None and st not in DEATH_STATES \
                        and st != 0x00:
                    prev, armed = st, True
                    print(f"\r    t={t:>7.2f}  armed at {state_name(st)}"
                          f"{' ' * 30}")
            elif st is not None and st != prev:
                kind = None
                if prev in DEATH_STATES:
                    kind = "(leaving a death -- not a new one)"
                elif lid in BOSS_LEVELS and st in (DROWN, FALL, CRUSH, VOID):
                    kind = "(boss arena: the void is a phase change)"
                elif st == CAUGHT:
                    kind = ">>> DEATHLINK: captures"
                elif st == DROWN:
                    kind = ">>> DEATHLINK: drown"
                elif prev in TRANSFORM_STATES and st == 0x00 and not loading:
                    kind = (">>> DEATHLINK: void_out  (died while "
                            "transformed -- no death state at all)")
                elif st in (FALL, CRUSH, VOID):
                    kind = ">>> DEATHLINK: void_out (subject to the amnesty)"
                line = (f"    t={t:>7.2f}  {state_name(prev):<28} -> "
                        f"{state_name(st):<28} costume=0x{cos:02X}"
                        if cos is not None else
                        f"    t={t:>7.2f}  {state_name(prev):<28} -> "
                        f"{state_name(st):<28}")
                print(f"\r{line}  {kind or ''}{' ' * 8}")
                if kind and kind.startswith(">>>"):
                    fired.append((t, prev, st, kind))
                seq.append((t, prev, st))
                prev = st

            req = None
            ra = resolve_addr(pine, f"[[{TAZ_PTR:#x}]+{O_STATE_PTR:#x}]"
                                    f"+{S_REQUEST:#x}")
            if ra is not None:
                req = pine.read_u32(ra)
            sys.stdout.write(
                f"\r    t={t:>7.2f}  lvl={lid:<4} {state_name(st):<32} "
                f"costume={'--' if cos is None else f'0x{cos:02X}'} "
                f"request={'--' if req is None else f'0x{req:02X}'} "
                f"{'LOADING' if loading else '       '}  ")
            sys.stdout.flush()
            time.sleep(1.0 / hz)
    except KeyboardInterrupt:
        print("\n")

    print(f"    {len(seq)} state changes, {len(fired)} would have sent a "
          f"DeathLink.")
    for t, a, b, kind in fired:
        print(f"      t={t:.2f}  {state_name(a)} -> {state_name(b)}   {kind}")
    if not fired:
        print("      Nothing would have been sent. If you did die, the")
        print("      transition above shows what the game did instead --")
        print("      a state that is not in the death set never reports.")
    st_ = load_state()
    st_.setdefault("death", []).append(
        {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
         "changes": [[t, a, b] for t, a, b in seq[-60:]],
         "fired": [[t, a, b, k] for t, a, b, k in fired]})
    save_state(st_)
    return 0


BOUNTY_ADDRS = [
    ("CURRENT_BOUNTY  (pause menu)", 0x00507210),
    ("next to it       (pause menu)", 0x005073E0),
    ("popup on screen", 0x00409034),
    ("popup on screen", 0x0040C5A4),
    ("TOTAL_BOUNTY_LIVE", 0x003CA3A8),
    ("TOTAL_BOUNTY_SAVE", 0x0040403C),
]


def cmd_bounty(pine, args):
    """Every bounty number at once, live and saved.

    The four that were found by searching all change something on screen and
    none of them stick when written, which is the same shape destruction
    turned out to have. The two that have not been tried are the totals --
    the per-level one in the save block and the global one -- and a saved
    total is the kind of thing that CAN be given a head start.
    """
    here = pine.read_u32(LEVEL_ID)
    f = int(args.save_file)
    rows = list(BOUNTY_ADDRS)
    if FIRST_LEVEL <= here <= 20:
        rows.append((f"level {here} saved total",
                     level_block(here, f) + 0x218))
    print(f"    in level {here} ({LEVEL_NAMES.get(here, '?')}), save file {f}")
    print("    Ctrl-C to stop. Smash something and watch which move.")
    print()
    try:
        while True:
            cells = "  ".join(f"{n}=0x{a:08X}:{pine.read_u32(a)}"
                              for n, a in rows)
            sys.stdout.write("\r    " + cells + "   ")
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n")
    for n, a in rows:
        print(f"      {n:<30} 0x{a:08X} = {pine.read_u32(a)}")
    print()
    print("    Poke the two totals rather than the four that were found by")
    print("    searching -- those are what the game recomputes.")
    return 0


def cmd_poke(pine, args):
    """Write a value and see what the game does about it.

    The one experiment that separates "the clock ends it" from "a target ends
    it": put a number on the board that no rally could reach and watch whether
    the match stops. Close the AP client first -- a poke that ends the fight
    while the client is watching sends a DeathLink to everyone else.
    """
    expr, width = split_width(args.address)
    width = width or "u8"
    addr, value = resolve_addr(pine, expr), int(args.value, 0)
    if addr is None:
        print(f"    {args.address} does not resolve right now.")
        return 1
    before = read_as(pine, addr, width)
    if width == "u8":
        pine.write_u8(addr, value)
    elif width == "float":
        pine.write_u32(addr, struct.unpack("<I", struct.pack("<f", float(value)))[0])
    else:
        pine.write_u32(addr, value)
    time.sleep(0.2)
    after = read_as(pine, addr, width)
    print(f"    0x{addr:08X} ({width})  {before} -> wrote {value} -> reads {after}")
    if after != value:
        print("    It did not stick. Either the game rewrites it every frame")
        print("    (so this is a copy, not the counter) or the write missed.")
    print()
    print("    Watch the screen. If the match ends now, the fight has a score")
    print("    target and the clock is only a backstop. If it plays on with a")
    print("    silly number on the HUD, the clock is the only end condition.")
    return 0


def cmd_endwatch(pine, args):
    """Watch a whole fight end and report what moved.

    Answers the part that matters more than the scores: does the game record
    a RESULT anywhere? A byte that reads one thing after a win and another
    after a loss is worth more than two scores and a clock, because it is the
    game's own answer rather than our arithmetic about it.
    """
    bands = parse_bands(args.band) if args.band else (
        [WIDE_BAND] if args.full else list(WATCH_BANDS))
    hz = max(0.5, float(args.hz))
    total = sum(h - l for l, h in bands)
    print("    bands " + ", ".join(f"0x{l:08X}-0x{h:08X}" for l, h in bands)
          + f"  ({total / 1024:.0f} KB total), {hz:g} Hz")
    print("    Start the fight, then leave this running through the result")
    print("    and the screen that follows it. Ctrl-C to stop.")
    print()

    scores = list(args.score or [])
    if scores:
        print("    scoreline read from " + ", ".join(scores))
    base = {}
    changes = {}          # addr -> [(t, old, new), ...]
    marks = []
    t_start = time.time()
    last_print = 0.0
    try:
        while True:
            t = time.time() - t_start
            for lo, hi in bands:
                buf = read_block(pine, lo, hi - lo)
                old = base.get(lo)
                if old is None:
                    base[lo] = bytearray(buf)
                else:
                    diff_into(changes, lo, old, buf, round(t, 2))
            clock = pine.read_float(GLAD_TIMER)
            lid = pine.read_u32(LEVEL_ID)
            done = pine.read_u32(GLAD_COMPLETE)
            taz = pine.read_u32(TAZ_PTR)
            st = "--"
            if EE_MIN <= taz < EE_MAX:
                so = pine.read_u32(taz + O_STATE_PTR)
                if EE_MIN <= so < EE_MAX:
                    st = f"0x{pine.read_u8(so + S_STATE):02X}"
            sc = []
            for e in scores:
                a = resolve_addr(pine, e)
                sc.append(-1 if a is None else pine.read_u8(a))
            marks.append({"t": round(t, 2), "clock": round(clock, 3),
                          "level": lid, "complete": done, "state": st,
                          "scores": sc})
            if t - last_print >= 1.0:
                last_print = t
                board = "-".join(str(v) for v in sc) if sc else "--"
                sys.stdout.write(
                    f"\r    t={t:6.1f}s  clock={clock:8.2f}  level={lid:<3}"
                    f" state={st}  score={board:<8} complete={done}"
                    f"  moved={len(changes):<6}")
                sys.stdout.flush()
            time.sleep(max(0.0, 1.0 / hz - (time.time() - t_start - t)))
    except KeyboardInterrupt:
        print("\n")

    print(f"    {len(changes)} addresses moved during the fight.")
    print()

    # Before any address hunting: what did the game itself say? Every other
    # boss announces a loss with state 0x5A and this one reportedly never
    # does, so the states it DOES visit are worth seeing plainly.
    seen, order = {}, []
    for m in marks:
        s = m["state"]
        if s not in seen:
            seen[s] = [m["t"], m["t"]]
            order.append(s)
        else:
            seen[s][1] = m["t"]
    print("    Taz states visited, in the order they first appeared:")
    for s in order:
        first, last = seen[s]
        note = "   <- the loss state every other boss uses" if s == "0x5A" else ""
        print(f"      {s}   first t={first:.1f}s  last t={last:.1f}s{note}")
    print()

    if scores:
        print("    The scoreline, every time it moved:")
        prev_sc, last_move = None, None
        for m in marks:
            if prev_sc is not None and m["scores"] != prev_sc:
                arrow = "-".join(str(v) for v in m["scores"])
                print(f"      t={m['t']:>7.2f}s  clock={m['clock']:>8.2f}   "
                      f"{'-'.join(str(v) for v in prev_sc)} -> {arrow}")
                last_move = m
            prev_sc = m["scores"]
        end = marks[-1]["scores"] if marks else []
        print(f"      final {'-'.join(str(v) for v in end)}"
              + (f", last moved at t={last_move['t']:.2f}s "
                 f"clock={last_move['clock']:.2f}" if last_move else ""))
        print("      The clock at that last move is when the fight really")
        print("      stopped -- the clock keeps counting afterwards.")
        print()

    print("    Transitions in the things that are already understood:")
    transitions, prev = [], None
    for m in marks:
        cur = (m["level"], m["complete"], m["state"])
        if prev is not None and cur != prev:
            transitions.append({"t": m["t"], "clock": m["clock"],
                                "from": list(prev), "to": list(cur)})
            print(f"      t={m['t']:>7.2f}s  clock={m['clock']:>8.2f}  "
                  f"level {prev[0]}->{cur[0]}  complete {prev[1]}->{cur[1]}  "
                  f"state {prev[2]}->{cur[2]}")
        prev = cur
    if not transitions:
        print("      (nothing moved -- the fight may not have ended yet)")
    print()
    print("    Most interesting first -- addresses that changed exactly once,")
    print("    late, and then held. That is the shape of a result flag.")
    once = [(a, h) for a, h in changes.items() if len(h) == 1]
    once.sort(key=lambda kv: -kv[1][0][0])
    for a, h in once[:30]:
        t, old, new = h[0]
        print(f"      0x{a:08X}  at t={t:>7.2f}s   {old:>3} -> {new:<3}")
    if not once:
        print("      (none -- everything that moved kept moving)")

    print()
    print("    Small counters -- addresses that only ever held 0..15 and")
    print("    changed a handful of times. That is the shape of a score.")
    small = []
    for a, h in changes.items():
        vals = {h[0][1]} | {n for _, _, n in h}
        if max(vals) <= 15 and 1 <= len(h) <= 30:
            small.append((a, h, sorted(vals)))
    small.sort(key=lambda kv: len(kv[1]))
    for a, h, vals in small[:30]:
        seq = " ".join(str(n) for _, _, n in h[:14])
        print(f"      0x{a:08X}  {len(h):>3} changes  values {vals}   {seq}")
    if not small:
        print("      (none)")

    st = load_state()
    st.setdefault("endwatch", []).append({
        "bands": [[l, h] for l, h in bands],
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "result": args.result,
        "states": {s: seen[s] for s in order},
        "transitions": transitions,
        "timeline": marks[-40:],
        "once": [[a, h[0]] for a, h in once[:200]],
        "small": [[a, [list(x) for x in h]] for a, h, _ in small[:200]],
    })
    save_state(st)
    print()
    print("    Run this once for a fight you WIN and once for a fight you")
    print("    LOSE, passing --result win / --result loss, and the address")
    print("    that differs between the two runs is the result flag.")
    return 0


def cmd_compare(pine, args):
    """Intersect the endwatch runs: what a win did that a loss did not."""
    st = load_state()
    runs = st.get("endwatch", [])
    wins = [r for r in runs if r.get("result") == "win"]
    losses = [r for r in runs if r.get("result") == "loss"]
    if not wins or not losses:
        print(f"    have {len(wins)} win run(s) and {len(losses)} loss run(s);")
        print("    need at least one of each. Re-run `endwatch --result ...`.")
        return 1

    def final(run):
        out = {}
        for a, h in run["once"]:
            out[a] = h[2]
        for a, h in run["small"]:
            out[a] = h[-1][2]
        return out

    w = final(wins[-1])
    l = final(losses[-1])
    both = set(w) & set(l)
    differ = sorted(a for a in both if w[a] != l[a])
    print(f"    {len(both)} addresses moved in both runs, "
          f"{len(differ)} ended differently:")
    for a in differ[:40]:
        print(f"      0x{a:08X}   win -> {w[a]:<4} loss -> {l[a]}")
    only_w = sorted(set(w) - set(l))
    print()
    print(f"    {len(only_w)} moved only on the win:")
    for a in only_w[:20]:
        print(f"      0x{a:08X}   -> {w[a]}")
    only_l = sorted(set(l) - set(w))
    print()
    print(f"    {len(only_l)} moved only on the loss:")
    for a in only_l[:20]:
        print(f"      0x{a:08X}   -> {l[a]}")
    print()
    print("    An address in the first list, holding a small distinct value")
    print("    each way, is the result. Confirm it by fighting again and")
    print("    watching it with `watch`.")
    return 0


def cmd_hunt(pine, args):
    """Find Taz's counter by sweeping a window that follows Daffy.

    Daffy resolves off the base and Taz does not, but Taz is always some
    distance below him:

        load A   Daffy - Taz = 0x5BFC0        k=0
        load B   Daffy - Taz = 0x5C0A0        one 0xE0 record further along

    The first version of this sampled only points on that 0xE0 lattice,
    +/-6 of them, which is +/-1344 bytes -- and a load where Taz sat further
    out came back with nothing at all. A contiguous sweep of the same region
    costs one extra batched read and cannot miss for that reason, so the
    lattice is now something the result is CHECKED against rather than
    something the search assumes.

    Everything is measured as an offset from the anchor, re-resolved every
    tick, so a base that moves mid-fight does not silently turn this into a
    comparison between two different pieces of memory.
    """
    sep = int(args.sep, 0)
    width = int(args.width, 0)
    stride = int(args.stride, 0)
    hz = max(1.0, float(args.hz))
    print(f"    anchor {args.anchor}, sweeping {width // 1024}KB centred "
          f"{sep:#x} below it")
    print("    Start this BEFORE the fight so it sees the scoreline at 0.")
    print("    Ctrl-C to stop and see what moved.")
    print()

    changes = {}          # offset from anchor -> [(t, old, new), ...]
    base_line = None
    anchor_hist, anchor_addrs = [], []
    ticks = unresolved = rebased = 0
    t_start = time.time()
    try:
        while True:
            t = round(time.time() - t_start, 2)
            a = resolve_addr(pine, args.anchor)
            if a is None:
                unresolved += 1
                sys.stdout.write(
                    f"\r    {args.anchor} does not resolve "
                    f"({unresolved} ticks)          ")
                sys.stdout.flush()
                time.sleep(1.0 / hz)
                continue
            if not anchor_addrs or anchor_addrs[-1] != a:
                if anchor_addrs:
                    rebased += 1
                    base_line = None      # the window moved; start again
                anchor_addrs.append(a)
            av = pine.read_u8(a)
            if not anchor_hist or anchor_hist[-1] != av:
                anchor_hist.append(av)

            lo = a - sep - width // 2
            lo = max(EE_MIN, lo)
            hi = min(EE_MAX, lo + width)
            buf = read_block(pine, lo, hi - lo)
            if base_line is None:
                base_line = bytearray(buf)
            else:
                raw = {}
                diff_into(raw, lo, base_line, buf, t)
                for addr, evs in raw.items():
                    changes.setdefault(addr - a, []).extend(evs)
            ticks += 1
            live = sum(1 for v in changes.values() if len(v) >= 1)
            sys.stdout.write(
                f"\r    clock={pine.read_float(GLAD_TIMER):7.2f}  "
                f"anchor@0x{a:08X}={av:<4} moved={live:<5} ticks={ticks:<5}")
            sys.stdout.flush()
            time.sleep(1.0 / hz)
    except KeyboardInterrupt:
        print("\n")

    print(f"    {ticks} samples, {unresolved} with no anchor, "
          f"{rebased} anchor move(s) mid-run.")
    print(f"    anchor addresses: "
          + ", ".join(f"0x{x:08X}" for x in anchor_addrs[:6])
          + (" ..." if len(anchor_addrs) > 6 else ""))
    print(f"    anchor values: {' '.join(str(v) for v in anchor_hist[:24])}")
    print()

    if not changes:
        print("    Nothing in the window moved. If the anchor never resolved,")
        print("    the base at 0x3FF064 is the problem, not the window. If it")
        print("    did resolve and Daffy's own value never moved either, the")
        print("    fight was not running. Otherwise widen with --width.")
    else:
        print("    Candidates, most score-like first. A scoreline starts at 0,")
        print("    stays small, and changes a handful of times.")
        ranked = []
        for off, evs in changes.items():
            vals = [evs[0][1]] + [n for _, _, n in evs]
            small = max(vals) <= 30
            starts0 = vals[0] == 0
            ranked.append((not (small and starts0), -len(evs), off, evs, vals))
        ranked.sort()
        for _, _, off, evs, vals in ranked[:15]:
            d = -off
            on = "" if (d - sep) % stride else \
                f"   k={(d - sep) // stride:+d} on the {stride:#x} lattice"
            print(f"      anchor - {d:#x}   {len(evs)} changes"
                  f"   {' '.join(str(v) for v in vals[:16])}{on}")

    st = load_state()
    st.setdefault("hunt", []).append({
        "anchor": args.anchor, "sep": sep, "width": width, "stride": stride,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ticks": ticks, "unresolved": unresolved, "rebased": rebased,
        "anchor_addrs": anchor_addrs[:8], "anchor_values": anchor_hist[:32],
        # Only the best-ranked few hundred: a 64KB window in a busy frame can
        # produce thousands of moving bytes, and a json that size is unusable.
        "moved": {str(off): [list(e) for e in evs[:40]]
                  for *_, off, evs, _ in (ranked[:200] if changes else [])}})
    save_state(st)
    print()
    print("    Run it again next load. The distance that comes back both")
    print("    times, or the rule that predicts it, is what boss_lost needs.")
    return 0


def dedupe(seq):
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def cmd_dump(pine, args):
    """Hex and ASCII around an address, to see what kind of record it is in.

    If Taz's counter and Daffy's counter are the same field in two copies of
    one structure, the bytes around them will rhyme -- and this game tags its
    actor objects with four ASCII characters, so a tag in the dump is a
    signature the client could search for instead of guessing a slot.
    """
    a = resolve_addr(pine, args.address)
    if a is None:
        print(f"    {args.address} does not resolve right now.")
        return 1
    before, after = int(args.before, 0), int(args.after, 0)
    lo = max(EE_MIN, (a - before) & ~0xF)
    hi = min(EE_MAX, a + after)
    raw = read_block(pine, lo, hi - lo)
    print(f"    {args.address} -> 0x{a:08X}")
    print()
    if args.words:
        for off in range(0, len(raw) - 3, 4):
            addr = lo + off
            v = int.from_bytes(raw[off:off + 4], "little")
            tag = ""
            if 0x00100000 <= v < 0x02000000:
                s = _read_w(pine, v, 28)
                if s.strip("."):
                    tag = f"  -> {s!r}"
            mark = "  <<<" if addr <= a < addr + 4 else ""
            print(f"      0x{addr:08X}  {v:>11}  0x{v:08X}{tag}{mark}")
        return 0
    for off in range(0, len(raw), 16):
        row = raw[off:off + 16]
        addr = lo + off
        hexs = " ".join(f"{b:02X}" for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        mark = " <<<" if addr <= a < addr + 16 else ""
        print(f"      0x{addr:08X}  {hexs:<47}  {text}{mark}")
    return 0


def cmd_watch(pine, args):
    """Print a few addresses live, next to the clock and the state.

    Each one is re-resolved every tick, so a chain that moves is followed
    rather than frozen at whatever it read when the command started.
    """
    print("    Ctrl-C to stop.")
    print()
    try:
        while True:
            parts = []
            for e in args.addresses:
                expr, width = split_width(e)
                a = resolve_addr(pine, expr)
                if a is None:
                    parts.append(f"{e}=??")
                elif width:
                    parts.append(f"{e}@0x{a:08X}={read_as(pine, a, width)}")
                else:
                    # No width asked for, so show both readings -- a byte and
                    # a word disagree loudly when the guess is wrong.
                    parts.append(f"{e}@0x{a:08X}=u8:{pine.read_u8(a)}"
                                 f"/u32:{pine.read_u32(a)}")
            vals = "  ".join(parts)
            taz = pine.read_u32(TAZ_PTR)
            stv = "--"
            if EE_MIN <= taz < EE_MAX:
                so = pine.read_u32(taz + O_STATE_PTR)
                if EE_MIN <= so < EE_MAX:
                    stv = f"0x{pine.read_u8(so + S_STATE):02X}"
            sys.stdout.write(
                f"\r    clock={pine.read_float(GLAD_TIMER):8.2f}  "
                f"lvl={pine.read_u32(LEVEL_ID):<3} state={stv} "
                f"done={pine.read_u32(GLAD_COMPLETE)}  {vals}   ")
            sys.stdout.flush()
            time.sleep(1.0 / max(1.0, float(args.hz)))
    except KeyboardInterrupt:
        print("\n")
    return 0


# ---------------------------------------------------------------- entry

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Find what decides the Gladiatoons fight in Taz: Wanted (PS2).")
    p.add_argument("--slot", type=int, default=28011)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="connect and print every known landmark")

    f = sub.add_parser("find", help="narrow the band down to the two scores")
    f.add_argument("--full", action="store_true", help="search all 32MB")
    f.add_argument("--near", action="store_true",
                   help="only the 48KB around the other bosses' counters")

    an = sub.add_parser("anchor",
                        help="find a score's offset from a live base pointer")
    an.add_argument("--base", default=hex(DAFFY_BASE),
                    help="address holding the base pointer (default 0x3FF064)")
    an.add_argument("--window", default="0x80000",
                    help="how far either side of the base to search")
    an.add_argument("--who", default="Taz")
    an.add_argument("--fresh", action="store_true",
                    help="discard saved runs for this side and start over")
    an.add_argument("--known-offset", type=lambda s: int(s, 0),
                    default=DAFFY_OFF,
                    help="an offset already believed good, printed each round "
                         "as a sanity check (default Daffy's 0x678C)")

    tr = sub.add_parser("track",
                        help="find one on-screen number by typing it")
    tr.add_argument("--what", default="value",
                    help="what you are typing, e.g. destruction")
    tr.add_argument("--full", action="store_true")
    tr.add_argument("--near", action="store_true")

    sw = sub.add_parser("sandwiches",
                        help="show or set a level's sandwich count")
    sw.add_argument("--level", default="")
    sw.add_argument("--set", default="")
    sw.add_argument("--save-file", default="0")
    sw.add_argument("--find", action="store_true",
                    help="collect one sandwich and see which word counts it")
    sw.add_argument("--band", nargs=2, metavar=("LO", "HI"))
    sw.add_argument("--timeout", default="120")

    ms = sub.add_parser("message", help="show or hide a message box (WRITES)")
    ms.add_argument("--hide", action="store_true")

    mb = sub.add_parser("msgbox",
                        help="read the box's pointers, or put your own text up")
    mb.add_argument("--ptr", default="",
                    help="point the slots at a string that already exists")
    mb.add_argument("--text", default="",
                    help="write this into scratch memory and show it (WRITES)")
    mb.add_argument("--scratch", default="",
                    help="where to put --text, if you do not want it guessed")
    mb.add_argument("--slots", default="all",
                    help="'all' or a slot number 0-2")
    mb.add_argument("--flag", default="",
                    help="also write this to 0x0050900C")
    mb.add_argument("--inspect", default="",
                    help="hex, both encodings, and every word dereferenced")
    mb.add_argument("--bytes", default="0x60",
                    help="how much --inspect should read")
    mb.add_argument("--encoding", default="auto",
                    choices=["auto", "ascii", "utf16"],
                    help="how --text is written (auto copies --like)")
    mb.add_argument("--like", default="0x006470D0",
                    help="a pointer known to work, for --encoding auto")

    an_ = sub.add_parser("announce",
                         help="show a message safely and put it all back")
    an_.add_argument("text", nargs="?", default="Taz found an item!")
    an_.add_argument("--seconds", default="3.0")
    an_.add_argument("--word", default="Yes")
    an_.add_argument("--scratch", default="")
    an_.add_argument("--test", action="store_true",
                     help="the whole rehearsal, including what must fail")
    an_.add_argument("--via", default="0x006470D0",
                     help="slot value used to stage a competing box")

    mp = sub.add_parser("msgpoint",
                        help="change a word on screen by repointing its entry")
    mp.add_argument("--word", default="Yes",
                    help="the word as it appears on screen, capitals and all")
    mp.add_argument("--text", default="",
                    help="what it should say instead (WRITES)")
    mp.add_argument("--entry", default="",
                    help="use this table entry rather than the first found")
    mp.add_argument("--scratch", default="")
    mp.add_argument("--band", nargs=2, metavar=("LO", "HI"))
    mp.add_argument("--restore", action="store_true",
                    help="put the entry back the way it was")

    mt = sub.add_parser("msgtext",
                        help="put your own words in the box (WRITES)")
    mt.add_argument("text", nargs="?", default="",
                    help="omit to just report the buffer and its room")
    mt.add_argument("--via", default="0x006470D0",
                    help="the slot value that reliably raises a box")
    mt.add_argument("--hold", default="2.0",
                    help="seconds to keep re-writing if the game repaints")
    mt.add_argument("--capacity", default="")

    mi = sub.add_parser("msgids",
                        help="is the slot a number? walk ids and read back")
    mi.add_argument("--start", default="0")
    mi.add_argument("--end", default="40")
    mi.add_argument("--scratch", default="")
    mi.add_argument("--delay", default="0.25")

    sb = sub.add_parser("subtitle",
                        help="read or change the subtitle on screen")
    sb.add_argument("--wait", default="")
    sb.add_argument("--id", default="", help="write this index into the live object")
    sb.add_argument("--text", default="", help="repoint its entry at your words")
    sb.add_argument("--lookup", default="", help="just read index N from the table")
    sb.add_argument("--scratch", default="")
    sb.add_argument("--hold", default="8")

    sl = sub.add_parser("sublearn",
                        help="copy a real subtitle's node and object")
    sl.add_argument("--wait", default="60")

    sr = sub.add_parser("subraise",
                        help="build a subtitle by hand and show it (RISKY)")
    sr.add_argument("id", nargs="?", default="")
    sr.add_argument("--seconds", default="4")
    sr.add_argument("--watch", action="store_true",
                    help="diff the objects the template points at, before/after")
    sr.add_argument("--header", action="store_true",
                    help="also lay down the allocator header, so free() works")
    sr.add_argument("--leave", action="store_true",
                    help="do NOT empty the list -- let the game end it")
    sr.add_argument("--original", action="store_true",
                    help="rebuild at the address the game's object used")
    sr.add_argument("--revert", action="store_true",
                    help="with --watch, put those words back afterwards")
    sr.add_argument("--both", action="store_true",
                    help="also empty the overlay list on the way out")

    sf = sub.add_parser("subfade",
                        help="apply a fade preset by hand (WRITES)")
    sf.add_argument("--preset", default="",
                    help="0x00474490 (default) or 0x004746A0")
    sf.add_argument("--target", default="", help="default 0x006466F0")
    sf.add_argument("--bytes", default="0x40")
    sf.add_argument("--dry-run", action="store_true")
    sf.add_argument("--undo", action="store_true")

    sub.add_parser("subclear", help="force both message lists empty (WRITES)")

    to_ = sub.add_parser("textowner",
                         help="find the words on screen and what points at them")
    to_.add_argument("words")
    to_.add_argument("--band", nargs=2, metavar=("LO", "HI"))
    to_.add_argument("--text-band", nargs=2, metavar=("LO", "HI"))
    to_.add_argument("--wait", default="")
    to_.add_argument("--full", action="store_true")

    mc = sub.add_parser("msgchase",
                        help="follow pointers from an address until text turns up")
    mc.add_argument("root")
    mc.add_argument("--depth", default="3")
    mc.add_argument("--width", default="0x80")
    mc.add_argument("--budget", default="400")
    mc.add_argument("--min-chars", default="3")

    mn = sub.add_parser("msgnode",
                        help="walk the message lists and dereference them")
    mn.add_argument("--container", default="")
    mn.add_argument("--max", default="8")
    mn.add_argument("--deep", action="store_true",
                    help="also dump what each node's value points at")
    mn.add_argument("--bytes", default="0x80")
    mn.add_argument("--chase", default="",
                    help="walk the live value for text, at this depth")
    mn.add_argument("--wait", default="",
                    help="poll until a message is queued, then dump at once")

    ml = sub.add_parser("msglife",
                        help="record the message system in time order")
    ml.add_argument("--seconds", default="60")
    ml.add_argument("--radius", default="0x60")
    ml.add_argument("--band", nargs=2, metavar=("LO", "HI"))

    mw = sub.add_parser("msgwatch",
                        help="find the word that picks WHICH message shows")
    mw.add_argument("--manual", action="store_true",
                    help="trigger the box by writing SHOW=1 instead of waiting")
    mw.add_argument("--compare", action="store_true",
                    help="what a real message set that a forced one did not")
    mw.add_argument("--radius", default="0x2000",
                    help="bytes either side of each flag (default 0x2000)")
    mw.add_argument("--band", nargs=2, metavar=("LO", "HI"),
                    help="watch this range instead of around the flags")
    mw.add_argument("--timeout", default="180")
    mw.add_argument("--dismiss", action="store_true",
                    help="record what the game writes when YOU close a box")
    mw.add_argument("--via", default="0x006470D0",
                    help="slot value used to raise the box for --dismiss")

    de = sub.add_parser("destruction",
                        help="live vs saved destruction for every level")
    de.add_argument("--save-file", default="0")

    sub.add_parser("verify", help="do the saved addresses survive a reload?")

    ps = sub.add_parser("ptrscan", help="find pointers to an address, rigorously")
    ps.add_argument("target", help="the score's address in THIS load, e.g. 0x1A2B3C4")
    ps.add_argument("--max-offset", default="0x8000")
    ps.add_argument("--full", action="store_true")
    ps.add_argument("--band", nargs=2, metavar=("LO", "HI"),
                    help="scan this range instead of the default")

    ew = sub.add_parser("endwatch", help="log a whole fight and its ending")
    ew.add_argument("--result", choices=["win", "loss", "unknown"],
                    default="unknown")
    ew.add_argument("--hz", default="3")
    ew.add_argument("--full", action="store_true")
    ew.add_argument("--band", nargs=2, action="append", metavar=("LO", "HI"),
                    help="watch this range instead of the defaults; repeatable")
    ew.add_argument("--score", nargs="*", default=list(DEFAULT_SCORE_EXPRS),
                    help="Taz then Daffy, as literals or chains "
                         "(default: both off the base at 0x3FF064)")

    dl = sub.add_parser("deathlink",
                        help="dry-run the boss_lost decision over a real fight")
    dl.add_argument("--end", default="120.0", help="the horn, in clock units")
    dl.add_argument("--settle", default="3.0",
                    help="how long the scores must hold still after the horn")
    dl.add_argument("--hz", default="10")
    dl.add_argument("--expected", choices=["win", "loss", "tie", "unknown"],
                    default="unknown",
                    help="what actually happened, so the rule can be graded")

    bs = sub.add_parser("bosses",
                        help="do the boss counters reset when the arena loads?")
    bs.add_argument("--hz", default="4")

    rp = sub.add_parser("replay",
                        help="re-run the rule over recorded fights")
    rp.add_argument("--settle", nargs="*", help="settle values to try")
    rp.add_argument("--end", default="120.0")

    dt = sub.add_parser("death",
                        help="dry-run the ordinary-death rule, naming states")
    dt.add_argument("--hz", default="10")

    bo = sub.add_parser("bounty", help="every bounty number at once")
    bo.add_argument("--save-file", default="0")

    ft = sub.add_parser("findtext", help="search RAM for a string")
    ft.add_argument("text")
    ft.add_argument("--full", action="store_true")
    ft.add_argument("--text-band", action="store_true",
                    help="only 0x6A0000-0x6C0000, the known tables")

    sq = sub.add_parser("sequence",
                        help="what changed FIRST, in time order")
    sq.add_argument("--band", nargs=2, metavar=("LO", "HI"),
                    default=["0x003FC000", "0x00400000"])
    sq.add_argument("--gap", default="0.15",
                    help="quiet time that separates one wave from the next")
    sq.add_argument("--waves", default="3")

    cp = sub.add_parser("capture",
                        help="record what changes when something happens")
    cp.add_argument("--band", nargs=2, metavar=("LO", "HI"),
                    default=["0x003C0000", "0x00420000"])
    cp.add_argument("--name", default="trigger")
    cp.add_argument("--replay", action="store_true",
                    help="write the recorded values back (WRITES)")
    cp.add_argument("--max", default="64")
    cp.add_argument("--fresh", action="store_true",
                    help="ignore the previous capture instead of intersecting")
    cp.add_argument("--interesting", action="store_true",
                    help="replay only the flag-shaped words")
    cp.add_argument("--only", nargs="*",
                    help="replay only these addresses")

    stb = sub.add_parser("strtable",
                         help="find the string-pointer table and its indices")
    stb.add_argument("target", help="the address of a string you have found")
    stb.add_argument("--band", nargs=2, metavar=("LO", "HI"))
    stb.add_argument("--text-lo", default="0x00600000",
                     help="what counts as a plausible string address")
    stb.add_argument("--text-hi", default="0x00800000")
    stb.add_argument("--show", default="6", help="entries either side")
    stb.add_argument("--stride", default="",
                     help="force the entry size instead of guessing")
    stb.add_argument("--tables", default="2")

    rt = sub.add_parser("readtext", help="read a UTF-16LE string")
    rt.add_argument("address")
    rt.add_argument("--chars", default="120")

    wt = sub.add_parser("writetext", help="write a UTF-16LE string (WRITES)")
    wt.add_argument("address")
    wt.add_argument("text")
    wt.add_argument("--capacity", default="")

    ub = sub.add_parser("unball",
                        help="rehearse the ball recovery (WRITES)")
    ub.add_argument("--hz", default="10")
    ub.add_argument("--hold", default="2.0")

    sb = sub.add_parser("saveblock",
                        help="dump a level's save block and diff it")
    sb.add_argument("--level", default="14")
    sb.add_argument("--save-file", default="0")
    sb.add_argument("--label", default="")

    pk = sub.add_parser("poke", help="write a value and watch what the game does")
    pk.add_argument("address")
    pk.add_argument("value")

    sub.add_parser("compare", help="what a win did that a loss did not")

    la = sub.add_parser("hunt", aliases=["lattice"],
                        help="sweep for Taz's counter in a window that "
                             "follows Daffy")
    la.add_argument("--anchor", default=DEFAULT_SCORE_EXPRS[1],
                    help="the side that DOES resolve (default Daffy's chain)")
    la.add_argument("--sep", default=hex(SCORE_SEPARATION),
                    help="how far below the anchor to centre the sweep")
    la.add_argument("--width", default="0x10000",
                    help="how much to sweep, centred on that point")
    la.add_argument("--stride", default=hex(RECORD_STRIDE),
                    help="record size, used only to annotate the result")
    la.add_argument("--hz", default="4")

    du = sub.add_parser("dump", help="hex and ASCII around an address")
    du.add_argument("address")
    du.add_argument("--before", default="0x80")
    du.add_argument("--after", default="0x80")
    du.add_argument("--words", action="store_true",
                    help="one 32-bit word per line instead of a hex dump")

    w = sub.add_parser("watch", help="print addresses live")
    w.add_argument("addresses", nargs="+")
    w.add_argument("--hz", default="5")

    args = p.parse_args(argv)
    pine = Pine(args.slot)
    try:
        pine.connect()
    except ConnectionError as e:
        print(f"    {e}")
        return 1
    try:
        calibrate(pine, quiet=(args.cmd not in ("check", "find", "ptrscan")))
        return {
            "check": cmd_check, "find": cmd_find, "verify": cmd_verify,
            "anchor": cmd_anchor, "track": cmd_track,
            "destruction": cmd_destruction, "death": cmd_death,
            "sandwiches": cmd_sandwiches, "message": cmd_message,
            "msgwatch": cmd_msgwatch, "msgbox": cmd_msgbox,
            "msglife": cmd_msglife, "msgnode": cmd_msgnode,
            "msgchase": cmd_msgchase, "textowner": cmd_textowner,
            "subtitle": cmd_subtitle, "sublearn": cmd_sublearn,
            "subraise": cmd_subraise, "subclear": cmd_subclear,
            "subfade": lambda p, a: (cmd_subundo(p, a) if a.undo
                                     else cmd_subfade(p, a)),
            "msgids": cmd_msgids, "msgtext": cmd_msgtext,
            "msgpoint": cmd_msgpoint, "announce": cmd_announce,
            "bounty": cmd_bounty, "saveblock": cmd_saveblock,
            "unball": cmd_unball, "findtext": cmd_findtext,
            "readtext": cmd_readtext, "writetext": cmd_writetext,
            "strtable": cmd_strtable, "capture": cmd_capture,
            "sequence": cmd_sequence,
            "ptrscan": cmd_ptrscan, "endwatch": cmd_endwatch,
            "compare": cmd_compare, "watch": cmd_watch, "poke": cmd_poke,
            "hunt": cmd_hunt, "lattice": cmd_hunt, "dump": cmd_dump,
            "deathlink": cmd_deathlink, "replay": cmd_replay,
            "bosses": cmd_bosses,
        }[args.cmd](pine, args)
    finally:
        pine.close()


if __name__ == "__main__":
    raise SystemExit(main())
