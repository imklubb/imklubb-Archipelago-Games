"""In-game notification text for Taz: Wanted.

The game raises a subtitle by calling

    raise_subtitle(a0 = string index, a1 = flags, f12 = duration)   0x002C56E8

and its per-frame tick opens the panel, runs the timer, and tears the whole
thing down through end_message. Every allocation belongs to the game; nothing
here is forged and nothing leaks.

We could never CALL that function, so the tick's two call sites -- both the
word 0x0C0B160E with a nop in the delay slot -- are repointed at a trampoline
written into scratch RAM. It checks a control word, calls raise_subtitle when
it is set, and tail-calls the real tick:

    addiu sp, sp, -0x20        control block
    sw    ra, 0x10(sp)           +0x00  request, write 1
    lui   t0, hi                 +0x04  string index
    ticks++                      +0x08  flags (bit 1 -> slot A)
    lw    t1, request            +0x0C  duration, float bits
    beq   t1, zero, done         +0x10  last object raised
    sw    zero, request          +0x14  tick counter
    lw    a0/a1/t2               +0x18  raise counter
    mtc1  t2, $f12
    jal   raise_subtitle
    sw    v0, last;  raises++
    done: lw ra, 0x10(sp)
    j     the real tick
    addiu sp, sp, 0x20

If PCSX2 has not recompiled the patched block yet it keeps running the old
translation, which calls the real tick -- so the failure mode is that nothing
appears, never a crash. `ensure` notices and re-patches.

The words themselves come from the string table, which stores a pointer and a
length per entry, so entry 156 (empty in the game's own data) is pointed at a
scratch buffer we rewrite per message. The renderer resolves the index once
when the panel opens, so the buffer must be written first -- it is.
"""

import json
import logging
import os
import struct
import time

# Everything this module has to say is for the log file, not the player.
# The notifications themselves are the user-facing part; a client window
# narrating them alongside is just noise.
log = logging.getLogger("Client")

try:
    from . import pcsx2_mem as mem
except ImportError:                       # running the file directly
    import pcsx2_mem as mem


# ------------------------------------------------------------------ layout

TICK = 0x002C5838
RAISE_SUBTITLE = 0x002C56E8
CALL_SITES = (0x002827D8, 0x002BC968)
ORIGINAL_JAL = 0x0C000000 | (TICK >> 2)          # 0x0C0B160E

CTRL = 0x01F00900
CODE = 0x01F00940          # control block is 0x40 bytes
SCRATCH_LO, SCRATCH_HI = 0x01F00000, 0x01F02000
TEXT_BUF = 0x01F01000
TEXT_CAP = 200                                   # characters

C_REQUEST, C_INDEX, C_FLAGS = 0x00, 0x04, 0x08
C_DURATION, C_LAST, C_TICKS, C_RAISES = 0x0C, 0x10, 0x14, 0x18

# A general "call this function once" slot, for anything the game can do
# that we cannot reach from outside. Write the two arguments and then the
# function; the trampoline calls it on the next frame and clears the slot.
C_CALL_FN, C_CALL_A0, C_CALL_A1 = 0x1C, 0x20, 0x24
C_CALL_RET, C_CALLS = 0x28, 0x2C

STR_TABLE = 0x0069D250
BORROW_ID = 156                                  # empty in the game's data
STR_COUNT_PTR, STR_COUNT_OFF = 0x00413DFC, 0x24

LIST_A = 0x00508FE0
LIST_COUNT = 0x30
SLOTS = {2: 0x004746A0, 0: 0x00474490}
SLOT_OPEN = 0x194

# ------------------------------------------------------- the game's slowdowns
#
# The game slows time down for its own set pieces and puts text on screen
# while it does, so a notification raised during one lands on top of it.
#
# There is a single engine-wide time scale. SetTimeScale (0x002C8DD0) stores
# what it was asked for at 0x004752B8 and tail-calls 0x002C9198, which copies
# it to TIME_SCALE at 0x002C91B8. That copy is the live one: the frame-time
# routine loads it at 0x00285A20 and multiplies it into the delta at
# 0x00285A28. So "the game is in slow motion" is a float compare against 1.0,
# whoever asked for it -- no list of triggers to keep up to date.
#
# Exactly two callers ever pass less than 1.0, and both are the case at hand:
#
#   * the bounty/cash banner at 0x00201DD0. It writes 0.25 at 0x00201F58,
#     decays toward a 0.1 floor, holds, then ramps back and calls
#     SetTimeScale(1.0) at 0x002027A0 while clearing POPUP_STATE at
#     0x002027A4. The string-421 (Golden Sam Statue) and string-425
#     (destruction bonus) sites are the two that were found in the dump, but
#     it is the bounty-award popup in general -- smashing a Wanted Poster
#     raises it too, which was found by RECORDING rather than by reading, and
#     is the argument for gating on the mechanism instead of a list of
#     triggers that would have been wrong on day one.
#   * the West boss, SetTimeScale(0.5) at 0x00190240 and 0x001904C8.
#
# Two real ones, measured with taz_slowmo.py while mashing X to skip them:
# a statue ran 5.09s and a poster 2.91s, both 0.245 -> 0.10 -> ramp -> 1.0
# through banner states 2, 3, 4, 5, 0.
#
# POPUP_STATE is checked as well, and was expected to be the wider window --
# non-zero while the banner ramps its slowdown back out. At 30ms sampling it
# is not: the recording has scale and state going to 1.0 and 0 on the SAME
# sample. It stays because it costs one read and is the signal that actually
# means "the banner is up", where the scale only means "time is slow". The
# game has a getter for it at 0x00202100.
#
# Two things this deliberately does not use. GAME_STATE does not move -- it is
# written in one place, 0x00284E3C inside SetGameState, and none of that
# function's callers is in the banner or the boss, so it stays 1 (Active)
# throughout. And the 100-sandwich line (string 422) is not a slowdown at all:
# it goes to raise_subtitle at 0x0024C7E4, so LIST_A already covers it.
TIME_SCALE = 0x004125CC
POPUP_STATE = 0x003CA3B4
SLOW_BELOW = 0.999                # 1.0 with room for float noise

# The scale is back to 1.0 on the frame the banner tears down, but the words
# do not leave the screen on that frame. Waiting a beat costs nothing -- the
# queue is already holding -- and stops a notification appearing to interrupt
# the thing it was politely waiting for.
SLOW_SETTLE = 0.75
_slow_seen = 0.0

MSG_FLAGS = 2                                    # slot A, the tested one
MSG_SECONDS = 4.0

# Measured with taz_pad.py. The four shoulder buttons land on four bits of
# the top byte in exactly the PS2 hardware order, which is why this is the
# pad word and not a coincidence. Active low: idle has the bits set and
# pressing clears them. taz_pad.json overrides all three if it exists.
PAD_STATE = 0x00514FC0
PAD_L2, PAD_R2, PAD_L1, PAD_R1 = 0x01000000, 0x02000000, 0x04000000, 0x08000000
PAD_MASK = PAD_L1 | PAD_R1 | PAD_L2 | PAD_R2      # 0x0F000000
PAD_ACTIVE_LOW = True

# --------------------------------------------------------------- modes

OFF, PROGRESSION, ALL = 0, 1, 2
MODE_NAMES = {OFF: "Off", PROGRESSION: "Progressive", ALL: "All"}
MODE_TITLE = "In Game Text Client"
MODE_FROM_OPTION = {"off": OFF, "progressive": PROGRESSION, "all": ALL}
CYCLE = (OFF, PROGRESSION, ALL)

# Archipelago's item classification. 1 is progression; 0b010 useful, 0b100 trap.
FLAG_PROGRESSION = 0b001

# Shown in every mode except Off, whatever the seed classified it as.
# Wanted Posters are only progression up to the number the goal needs, but
# every one of them means the same thing to the player, so they all speak.
ALWAYS = -1


# ----------------------------------------------------------- trampoline

def _code_words(ctrl=CTRL):
    hi, lo = (ctrl >> 16) & 0xFFFF, ctrl & 0xFFFF

    def lw(rt, off):
        return 0x8C000000 | (8 << 21) | (rt << 16) | ((lo + off) & 0xFFFF)

    def sw(rt, off):
        return 0xAC000000 | (8 << 21) | (rt << 16) | ((lo + off) & 0xFFFF)

    return [
        0x27BDFFE0, 0xAFBF0010, 0x3C080000 | hi,          # 0  prologue
        lw(9, C_TICKS), 0x25290001, sw(9, C_TICKS),        # 3  ticks++
        lw(9, C_REQUEST), 0x11200000 | 13, 0x00000000,     # 6  subtitle?
        sw(0, C_REQUEST),                                  # 9
        lw(4, C_INDEX), lw(5, C_FLAGS), lw(10, C_DURATION),
        0x448A6000,                                        # 13 mtc1 f12
        0x0C000000 | (RAISE_SUBTITLE >> 2), 0x00000000,    # 14 jal
        0x3C080000 | hi, sw(2, C_LAST),
        lw(9, C_RAISES), 0x25290001, sw(9, C_RAISES),
        0x3C080000 | hi,                                   # 21 L_call
        lw(11, C_CALL_FN), 0x11600000 | 11, 0x00000000,    # 22 call?
        sw(0, C_CALL_FN),                                  # 25
        lw(4, C_CALL_A0), lw(5, C_CALL_A1),
        0x0160F809, 0x00000000,                            # 28 jalr t3
        0x3C080000 | hi, sw(2, C_CALL_RET),
        lw(9, C_CALLS), 0x25290001, sw(9, C_CALLS),
        0x8FBF0010, 0x08000000 | (TICK >> 2), 0x27BD0020,  # 35 done
    ]


def _read_words(addr, n):
    raw = mem.read_bytes(addr, 4 * n)
    return [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(n)]


def code_intact():
    try:
        return _read_words(CODE, len(_code_words())) == _code_words()
    except Exception:
        return False


def sites_patched():
    want = 0x0C000000 | (CODE >> 2)
    try:
        return [a for a in CALL_SITES if mem.read_u32(a) == want]
    except Exception:
        return []


def installed():
    return code_intact() and bool(sites_patched())


def install():
    """Write the trampoline and repoint both call sites. Idempotent.

    Refuses if a call site holds anything other than the original jal or our
    own -- a mismatch means the addresses are wrong for this build, and
    patching on a guess would be a crash rather than a missing message.
    """
    want = 0x0C000000 | (CODE >> 2)
    for a in CALL_SITES:
        w = mem.read_u32(a)
        # The original, ours, or an older build of ours -- a jal anywhere
        # into our own scratch is safe to overwrite, and that is what makes
        # changing the trampoline's layout a non-event.
        target = (w & 0x03FFFFFF) << 2
        ours = (w >> 26) == 3 and SCRATCH_LO <= target < SCRATCH_HI
        if w != ORIGINAL_JAL and not ours:
            raise RuntimeError(
                f"call site 0x{a:08X} holds 0x{w:08X}, expected "
                f"0x{ORIGINAL_JAL:08X}")
    words = _code_words()
    mem.write_bytes(CTRL, b"\0" * 0x40)
    mem.write_bytes(CODE, b"".join(struct.pack("<I", w) for w in words))
    if _read_words(CODE, len(words)) != words:
        raise RuntimeError("trampoline did not stay written")
    for a in CALL_SITES:
        mem.write_u32(a, want)
        if mem.read_u32(a) != want:
            raise RuntimeError(f"call site 0x{a:08X} did not stay patched")
    return True


def uninstall():
    for a in CALL_SITES:
        try:
            if mem.read_u32(a) != ORIGINAL_JAL:
                mem.write_u32(a, ORIGINAL_JAL)
        except Exception:
            pass


def call(fn, a0=0, a1=0, wait=1.0):
    """Have the game call fn(a0, a1) on its next frame. Returns v0.

    The one thing outside memory writes we could never do. Everything the
    game can do to itself is reachable through this -- as long as the
    arguments are real objects it already owns.
    """
    if not installed():
        return None
    if mem.read_u32(CTRL + C_CALL_FN):
        return None                       # a call is still pending
    before = mem.read_u32(CTRL + C_CALLS)
    mem.write_u32(CTRL + C_CALL_A0, a0)
    mem.write_u32(CTRL + C_CALL_A1, a1)
    mem.write_u32(CTRL + C_CALL_FN, fn)
    end = time.time() + wait
    while time.time() < end:
        if mem.read_u32(CTRL + C_CALLS) != before:
            return mem.read_u32(CTRL + C_CALL_RET)
        time.sleep(0.004)
    mem.write_u32(CTRL + C_CALL_FN, 0)
    return None


def ticks():
    try:
        return mem.read_u32(CTRL + C_TICKS)
    except Exception:
        return 0


# ------------------------------------------------------------- the words

# ------------------------------------------------- the game's own messages
#
# Three of the game's strings are lies under Archipelago. Each is raised when
# the player completes something that is now an AP CHECK, so what they are
# told about is a bonus game or a bounty that the seed may have replaced with
# somebody else's item entirely -- and the sandwich one appears whether or not
# a portal was ever granted.
#
#   422  "Congratulations! You've got 100 sandwiches! Now you can play the
#         bonus game!"
#   425  "Fantastic! You've reached your destruction bonus! "
#   421  "Well Done! You've found the secret item! But Sam's just raised the
#         bounty on you!"          <- the Golden Sam Statue
#
# Read out of the string table in a RAM dump, so these are the game's exact
# words rather than anyone's recollection of them.
#
# Each gets a scratch buffer of its own, 0x100 bytes apart, because all three
# can be pending in one level and a shared buffer would mean the last one
# written won every time.
BONUS_MSG = {
    "sandwich":    (422, 0x01F01200),
    "destruction": (425, 0x01F01300),
    "statue":      (421, 0x01F01400),
}
BONUS_CAP = 120                                  # characters per buffer

_bonus_saved = {}


def set_bonus_text(kind, text):
    """Point one of those messages at our words instead.

    This has to be in place BEFORE the box goes up. The renderer resolves a
    subtitle's id once, at raise time, so the moment to write is when the
    player enters the level -- not when the check fires, by which point the
    game has already read the entry and the old words are on screen.
    """
    spec = BONUS_MSG.get(kind)
    if spec is None or mem is None:
        return False
    sid, buf = spec
    if not (SCRATCH_LO <= buf < SCRATCH_HI - BONUS_CAP * 2 - 2):
        return False
    e = STR_TABLE + sid * 0x10
    try:
        if kind not in _bonus_saved:
            _bonus_saved[kind] = (mem.read_u32(e), mem.read_u32(e + 4))
        # Length in UTF-16 code units, not Python characters: a player name
        # with an emoji in it encodes to two units per character, and len()
        # would tell the renderer to stop halfway through.
        raw = text.encode("utf-16-le", "replace")[:BONUS_CAP * 2]
        if len(raw) % 2:
            raw = raw[:-1]
        mem.write_bytes(buf, raw + b"\0\0")
        mem.write_u32(e, buf)
        mem.write_u32(e + 4, len(raw) // 2)
        return True
    except Exception:
        return False


def restore_bonus_text():
    """Put the game's own strings back, for a clean disconnect.

    Only what was actually photographed before being overwritten -- writing a
    guessed pointer into the string table would be worse than leaving ours.
    """
    if mem is None:
        return
    for kind, was in list(_bonus_saved.items()):
        sid = BONUS_MSG[kind][0]
        e = STR_TABLE + sid * 0x10
        try:
            mem.write_u32(e, was[0])
            mem.write_u32(e + 4, was[1])
        except Exception:
            pass
    _bonus_saved.clear()


def bonus_line(kind, scout):
    """What the box should say instead.

    `scout` is (item name, player name) from Archipelago's LocationScouts, or
    None when the seed has no such check here or the scout has not come back.

    Phrased the same way whoever it belongs to -- including the player
    themselves. "You sent yourself" reads oddly for a moment and then reads
    correctly, and one sentence is easier to trust than two.
    """
    if scout:
        item, who = scout
        return f"You sent {who} their {item}!"
    return BONUS_FALLBACK.get(kind, "Congratulations, you sent an AP Item!")


# Used when the seed has no check for this thing in this level, or when the
# scout has not arrived yet. Deliberately still says an item was sent: the
# message only ever appears at the moment one is.
BONUS_FALLBACK = {
    "sandwich": "Congratulations, you found all 100 sandwiches and sent an "
                "AP Item!",
    "destruction": "Congratulations, you reached your destruction bonus and "
                   "sent an AP Item!",
    "statue": "Congratulations, you found the Golden Sam Statue and sent an "
              "AP Item!",
}


def _string_count():
    mgr = mem.read_u32(STR_COUNT_PTR)
    if not 0x00100000 <= mgr < 0x02000000:
        return 0
    return mem.read_u32(mgr + STR_COUNT_OFF)


def borrow_entry():
    """Point entry BORROW_ID at our scratch buffer and leave it there.

    Restoring it between messages would leave a window where the table is
    half-set, and the entry is empty in the game's own data, so nothing it
    raises on its own is disturbed. The original is returned for `release`.
    """
    e = STR_TABLE + BORROW_ID * 0x10
    was = (mem.read_u32(e), mem.read_u32(e + 4))
    if was[0] != TEXT_BUF:
        mem.write_u32(e, TEXT_BUF)
    return was


def release_entry(was):
    if not was:
        return
    e = STR_TABLE + BORROW_ID * 0x10
    try:
        mem.write_u32(e, was[0])
        mem.write_u32(e + 4, was[1])
    except Exception:
        pass


def _put_text(text):
    """Write the words and set the entry's length.

    The length is in UTF-16 code units, not Python characters -- a player
    name containing an emoji encodes to two units per character, and using
    len(text) would tell the renderer to stop halfway through.
    """
    raw = text.encode("utf-16-le", "replace")[:TEXT_CAP * 2]
    if len(raw) % 2:
        raw = raw[:-1]
    mem.write_bytes(TEXT_BUF, raw + b"\0\0")
    e = STR_TABLE + BORROW_ID * 0x10
    mem.write_u32(e, TEXT_BUF)
    mem.write_u32(e + 4, len(raw) // 2)
    return len(raw) // 2


# ------------------------------------------------------------ raising

def slowed(now=None):
    """True while the game is in slow motion, or for a beat afterwards.

    See the TIME_SCALE comment above for where these two addresses come from.
    The scale is the general signal and the banner state is the specific one;
    either being set means the game has its own words on screen.

    False on a read failure rather than True: a dead connection must not look
    like a permanent slowdown, and `idle` refuses on its own reads anyway.
    """
    global _slow_seen
    now = time.time() if now is None else now
    try:
        if mem.read_u32(POPUP_STATE) or mem.read_float(TIME_SCALE) < SLOW_BELOW:
            _slow_seen = now
            return True
    except Exception:
        return False
    return now - _slow_seen < SLOW_SETTLE


def idle():
    """True when a message can go up right now.

    Not just "is the game active" -- if anything is already on list A, ours
    would queue behind it and the two would overlap in the reading. One at a
    time reads better and costs nothing.

    The same argument covers the game's slow-motion set pieces, whose text
    does NOT go through list A: the bounty banner draws its own widget, so
    list A stays empty and a notification raised there lands straight on top
    of it. That is the one `slowed` catches.
    """
    try:
        if mem.read_u32(CTRL + C_REQUEST):
            return False
        if mem.read_u32(LIST_A + LIST_COUNT):
            return False
        if slowed():
            return False
        return mem.read_u32(SLOTS[MSG_FLAGS] + SLOT_OPEN) == 0
    except Exception:
        return False


def raise_text(text, seconds=MSG_SECONDS, flags=MSG_FLAGS):
    """Ask the game to raise one subtitle saying `text`. Returns True if the
    request was accepted; the game does everything after that."""
    if not text:
        return False
    if BORROW_ID >= _string_count():
        return False
    _put_text(text)
    mem.write_u32(CTRL + C_INDEX, BORROW_ID)
    mem.write_u32(CTRL + C_FLAGS, flags)
    mem.write_u32(CTRL + C_DURATION,
                  struct.unpack("<I", struct.pack("<f", float(seconds)))[0])
    mem.write_u32(CTRL + C_REQUEST, 1)
    return True


# --------------------------------------------------------------- hotkey

class Hotkey:
    """All four shoulder buttons at once, on the rising edge only.

    A held combo must fire exactly once, and the buttons will never land on
    the same frame, so the combo has to be seen settled for a moment before it
    counts -- otherwise pressing them one by one cycles the mode twice on the
    way in.
    """

    SETTLE = 0.12
    REARM = 0.40

    def __init__(self):
        self.down_since = None
        self.fired_at = 0.0

    @property
    def usable(self):
        return PAD_STATE is not None and PAD_MASK

    def held(self):
        try:
            v = mem.read_u32(PAD_STATE) & PAD_MASK
        except Exception:
            return False
        return v == 0 if PAD_ACTIVE_LOW else v == PAD_MASK

    def pressed(self):
        if not self.usable:
            return False
        now = time.time()
        if not self.held():
            self.down_since = None
            return False
        if self.down_since is None:
            self.down_since = now
            return False
        if now - self.down_since < self.SETTLE:
            return False
        if now - self.fired_at < self.REARM:
            return False
        self.fired_at = now
        self.down_since = now + 3600      # one fire per hold
        return True


# ------------------------------------------------------------- notifier

class Notifier:
    """Queue in, subtitles out.

    Nothing is shown unless the player actually has control: GAME_STATE 1,
    a save file loaded, and the attract-mode demo not running. Everything
    else -- paused, loading, cutscene, results, title -- holds the queue.
    """

    MAX_QUEUE = 40

    def __init__(self, mode=PROGRESSION):
        self.mode = mode
        self.queue = []
        self.hotkey = Hotkey()
        self.borrowed = None
        self.enabled = False
        self.failed = None
        self._last_try = 0.0
        self._warned_ticks = False

    # ------------------------------------------------------------ setup

    def attach(self):
        """Put the trampoline in place. Safe to call repeatedly."""
        if installed():
            self.enabled, self.failed = True, None
            if self.borrowed is None:
                self.borrowed = borrow_entry()
            return True
        try:
            install()
            self.borrowed = borrow_entry()
            self.enabled, self.failed = True, None
            log.debug("in-game text: trampoline attached")
            return True
        except Exception as exc:
            self.enabled = False
            if self.failed != str(exc):
                self.failed = str(exc)
                log.debug("in-game text unavailable: %s", exc)
            return False

    def detach(self):
        release_entry(self.borrowed)
        self.borrowed = None
        uninstall()
        self.enabled = False

    # ------------------------------------------------------------ input

    def wants(self, flags):
        if self.mode == OFF:
            return False
        if flags == ALWAYS or self.mode == ALL:
            return True
        return bool(flags & FLAG_PROGRESSION)

    def push(self, text, flags=FLAG_PROGRESSION):
        if not self.wants(flags):
            return False
        if len(self.queue) >= self.MAX_QUEUE:
            # Losing the oldest is better than a queue that takes minutes to
            # drain and reports things long after they happened.
            self.queue.pop(0)
        self.queue.append(text)
        return True

    def announce_mode(self):
        """Always shown, even when switching to Off -- otherwise turning it
        off looks identical to it being broken."""
        self.queue.insert(0, f"{MODE_TITLE}: {MODE_NAMES[self.mode]}")

    def cycle(self):
        self.mode = CYCLE[(CYCLE.index(self.mode) + 1) % len(CYCLE)]
        if self.mode == OFF:
            self.queue.clear()
        self.announce_mode()
        return self.mode

    # ------------------------------------------------------------- tick

    def tick(self, in_control):
        """One pass. `in_control` is the caller's judgement about whether the
        player can actually read a message right now."""
        out = []
        if self.hotkey.pressed():
            mode = self.cycle()
            log.debug("%s: %s", MODE_TITLE, MODE_NAMES[mode])

        if not self.queue:
            return out
        if not in_control:
            return out

        now = time.time()
        if not self.enabled:
            if now - self._last_try < 5.0:
                return out
            self._last_try = now
            if not self.attach():
                return out
        elif not installed():
            # A reset or a savestate load can take the patch away underneath
            # us. Notice rather than silently stop working.
            self.enabled = False
            self.borrowed = None
            return out

        if not self._warned_ticks and ticks() == 0:
            self._warned_ticks = True
            log.debug("in-game text: patched, but the trampoline has not "
                      "run yet -- it starts on the next level load")

        if not idle():
            return out
        text = self.queue[0]
        if raise_text(text):
            self.queue.pop(0)
        return out


# --------------------------------------------------------------- text

def ordinal(n):
    """1st, 2nd, 3rd, 4th ... and 11th/12th/13th, which are the ones a
    naive last-digit rule gets wrong."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def poster_line(count, sender_name):
    """Wanted Posters are counted, because which number it is IS the
    information -- the name alone never changes."""
    which = ordinal(count)
    if sender_name:
        return f"{sender_name} sent your {which} Wanted Poster!"
    return f"You received your {which} Wanted Poster!"


def item_line(item_name, sender_name, own):
    """What the box says.

    Kept short: the panel wraps, but a notification the player has to read
    twice defeats the point.
    """
    if own or not sender_name:
        return f"You received {item_name}!"
    return f"{sender_name} sent {item_name}!"


# ---------------------------------------------------- pad configuration

def configure_pad(path):
    """Override the pad constants from a json written by `taz_pad.py combo`.

    The built-in values are already the measured ones, so this only matters
    if the addresses ever move. Tried where the client runs first, then
    beside this file, because the launcher does not promise a directory.
    """
    global PAD_STATE, PAD_MASK, PAD_ACTIVE_LOW
    here = os.path.dirname(os.path.abspath(__file__))
    d = None
    for p in (path, os.path.join(here, os.path.basename(path))):
        try:
            with open(p) as fh:
                d = json.load(fh)
            break
        except Exception:
            continue
    if d is None:
        return False
    if "pad_state" not in d or "pad_mask" not in d:
        return False
    PAD_STATE = int(d["pad_state"])
    PAD_MASK = int(d["pad_mask"])
    PAD_ACTIVE_LOW = bool(d.get("pad_active_low", True))
    return True
