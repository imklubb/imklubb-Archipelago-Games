#!/usr/bin/env python3
"""One global that means "a boss game was just lost", on all five bosses.

    py -3.13 taz_continue.py check          verify the addresses vs the dump
    py -3.13 taz_continue.py watch          RECORD a real boss loss
    py -3.13 taz_continue.py watch --secs 600
    py -3.13 taz_continue.py watch --raw    every sample, not just changes

`watch` is the one that matters. Start it, lose a boss, and let the
"Continue this game" prompt run to an answer. Nothing is written; it is safe
on a real seed.

Close the AP client first -- PINE takes one connection at a time.


WHAT THIS WATCHES, AND WHY THAT ADDRESS
---------------------------------------
Read out of ee_dump.bin, instruction by instruction. Every claim below has
the address it came from, so none of it has to be taken on trust.

The game has a shared boss-loss mechanism and it is not the state machine.
`C:/Taz/Source/bossgamecontinue.cpp` (the path string is at 0x0049A738,
which is how the module was named at all) has one entry point:

    0x001A1B08  BossGameContinue::Start(handler, actor, 0, on_enter)

It `new`s a 60-byte object (0x001A1B68 calls the allocator at 0x002E4CF8)
and parks the pointer in one fixed global:

    0x001A1B70  sw $v0, -0x3c4($s0)      $s0 = 0x00380000  ->  0x0037FC3C

**0x0037FC3C is the whole signal.** Zero the entire rest of the time,
non-zero from the losing blow until the Continue prompt is answered.

It has exactly five writers: the one above, the destructor's null-out at
0x001A2550, and three in the finisher 0x001A27B8 (0x001A2988, 0x001A2994,
0x001A2998). Nothing else in the image writes it.

And 0x001A1B08 has exactly SEVEN callers, all of them inside the five boss
modules and nowhere else:

    0x00197D88  ZooBoss.cpp        level  7  Elephant Pong
    0x001A63FC  CityBoss.cpp       level 12  Gladiatoons
    0x00190E1C  WestBoss.cpp       level 17  Dodge City
    0x001A98DC  tazboss1.cpp       level 19  Disco Volcano
    0x0017E204  vehicles.cpp       level 20  The Hindenbird
    0x00184EA8  mtweetymagnet.cpp  level 20  The Hindenbird
    0x00187E2C  rocketscience.cpp  level 20  The Hindenbird

Not normal death, not the bonus games, not the two-player modes. That is
what makes it a general mechanism rather than a sixth special case.

It is polled every frame from the level tick's shared tail --
0x00228450 loads it, 0x00228454 branches past on zero, 0x0022845C calls
BossGameContinue::Update at 0x001A20B8 -- so it is live on every level, and
0x002B1680 reads it a second time as a "is a continue prompt up" guard.

The object, at [0x0037FC3C]:

    +0x00  the boss's own answer handler   (0x001A1B78 stores it)
    +0x04  on-enter callback               (0x001A1B80)
    +0x08  the actor                       (0x001A1B7C)
    +0x0C  THE ANSWER: 0 pending, 1 continue, 2 quit
           zeroed at 0x001A1B84, set to 1 at 0x001A2438, to 2 at 0x001A24F8
    +0x10  fade phase       +0x14/+0x1C/+0x20  fade value/target/rate
    +0x38  the popup object (0x001A2780), built by 0x001A2718 with string
           195, "Continue this game"

The symmetric WIN is just as shared: playerstats.cpp writes the save
block's complete flag at 0x002B555C and then calls SetGameState(.., 9) at
0x002B5508. Scanning all 66 callers of SetGameState, **0x002B5508 is the
only site in the image that passes 9.** So GAME_STATE == 9 is "a level was
completed", boss levels included.

The per-boss counters are kept below as a second opinion, all re-read from
the dump and all now exact:

  7  Elephant Pong  0x0037D8FC  Gossamer's score, SIGNED BYTE not u32
                    0x0037D3FC  Taz's score (the block is obj+0x680+p*0x500,
                                score at +0x29C; read at 0x00198D24 with lb)
                    target 3 in 1P, 5 in 2P (0x00198D14 / 0x00198D1C)
                    0x0037D130  winner index; 0x0037D134 loser
 12  Gladiatoons    0x00380978  Taz pods, 0x0038097C Daffy pods, both u32.
                    The end test is at 0x001A63B8-0x001A63C4:
                      lw Taz, lw Daffy, slt, beqz -> LOSE.
                    So Daffy >= Taz is a loss -- A DRAW IS A LOSS.
                    These are word mirrors the module rewrites every frame
                    from an actor chain (0x001A6028, 0x001A603C), which is
                    why the byte read and the word read are the same number.
                    0x00380E28 (the clock) is presentation only; the loss
                    test never reads it.
 17  Dodge City     0x0036BCD0  winner index, set to 1 at 0x0018E80C when
                    the shared helmet routine 0x00187198 returns 0.
                    Dodge City NEVER uses state 0x5A.
 19  Disco Volcano  0x00383EB0  Sam's score, u32, lose at >= 6 (0x001A98B8)
                    0x00381BF0  Taz's counterpart
 20  The Hindenbird state 0x5A, installed at 0x00184E44 -- the only site in
                    the image that installs it. 0x5A is the HINDENBIRD's
                    loss, not every boss's.

WHAT THE RECORDINGS ACTUALLY SAID
---------------------------------
Caleb ran `watch` against all five. Two results, both of which changed the
design:

  * **7, 12, 17 and 19 were picked up cleanly.** Gladiatoons included --
    the fight that had never once sent a DeathLink. That is the whole
    feature working.

  * **The Hindenbird offers no Continue screen at all**, so 0x0037FC3C
    never moves for level 20. It is not a wrong address and not a missed
    edge: losing to Tweety simply does not put the prompt up. So level 20
    keeps the old signal, state 0x5A -- which is consistent rather than
    awkward, because 0x00184E44 is the only site in the image that installs
    0x5A and it lives in mtweetymagnet.cpp. 0x5A was never the shared
    mechanism it was taken for; it is the Hindenbird's, and the Hindenbird
    is the one arena the shared mechanism does not reach.

    game.py ORs the two behind a single latch rather than switching on
    level id, so the Hindenbird cannot send twice if a phase ever does
    raise a prompt.

  * **Forcing a loss by writing the arena's own losing value does NOT
    work.** Writing Gossamer 3, Sam 6, or the Gladiatoons pod source did
    not make Taz lose -- the arena has to actually take the last hit or the
    last point for the game to consider itself finished. The freezes in
    BOSS_LOSS and HELMET_BOSSES are there for exactly that reason and they
    stay. Left as it was.

Two things this is NOT:

  * a replacement for death_tick(). This is boss games only.
  * the whole story for level 20. See above.
"""

import argparse
import importlib.util
import os
import struct
import sys
import time

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")
DUMP = os.path.join(HERE, "ee_dump.bin")

CONTINUE_PTR = 0x0037FC3C        # -> BossGameContinue, or 0. THE signal.
CONTINUE_ANSWER = 0x0C           # offset in it: 0 pending, 1 continue, 2 quit
CONTINUE_PHASE = 0x10            # fade phase

GAME_STATE = 0x003FF040          # 9 == level completed
LEVEL_BYTE = 0x0046DD5C          # current level id, one byte
TAZ_PTR = 0x003FF060
O_STATE_PTR = 0x1C8
S_STATE = 0x0B0

BOSS_LEVELS = {7: "Elephant Pong", 12: "Gladiatoons", 17: "Dodge City",
               19: "Disco Volcano", 20: "The Hindenbird"}

# per-boss counters, the second opinion
ZOO_THEIRS = 0x0037D8FC          # signed byte
ZOO_OURS = 0x0037D3FC            # signed byte
ZOO_WINNER = 0x0037D130
GLAD_OURS = 0x00380978
GLAD_THEIRS = 0x0038097C
GLAD_CLOCK = 0x00380E28
WEST_WINNER = 0x0036BCD0
DISCO_THEIRS = 0x00383EB0
DISCO_OURS = 0x00381BF0

# A sane main-RAM pointer. A load in progress can leave garbage in a global,
# and firing a DeathLink off garbage kills everyone else in the multiworld.
PTR_LO, PTR_HI = 0x00100000, 0x02000000


# The instructions every address above was read from. `check` asserts each
# still decodes to the same word, so a wrong build is caught before anything
# is believed.
EXPECT = [
    (0x001A1B70, 0xAE02FC3C, "sw $v0, -0x3c4($s0)      [0x0037FC3C] = new"),
    (0x001A1B84, 0xAC40000C, "sw $zero, 0xc($v0)       answer = 0 (pending)"),
    (0x001A2550, 0xAE00FC3C, "sw $zero, -0x3c4($s0)    destructor clears it"),
    (0x001A2998, 0xAEC0FC3C, "sw $zero, -0x3c4($s6)    finisher clears it"),
    (0x001A24F8, 0xAC62000C, "sw $v0, 0xc($v1)         answer = 2 (quit)"),
    (0x00228450, 0x8C62FC3C, "lw $v0, -0x3c4($v1)      the per-frame poll"),
    (0x0022845C, 0x0C06882E, "jal 0x1a20b8             ... -> ::Update"),
    (0x002B1680, 0x8C43FC3C, "lw $v1, -0x3c4($v0)      the second reader"),

    (0x00197D88, 0x0C0686C2, "jal 0x1a1b08             7  ZooBoss lost"),
    (0x001A63FC, 0x0C0686C2, "jal 0x1a1b08             12 CityBoss lost"),
    (0x00190E1C, 0x0C0686C2, "jal 0x1a1b08             17 WestBoss lost"),
    (0x001A98DC, 0x0C0686C2, "jal 0x1a1b08             19 tazboss1 lost"),
    (0x0017E204, 0x0C0686C2, "jal 0x1a1b08             20 vehicles lost"),
    (0x00184EA8, 0x0C0686C2, "jal 0x1a1b08             20 mtweetymagnet"),
    (0x00187E2C, 0x0C0686C2, "jal 0x1a1b08             20 rocketscience"),

    (0x002B5508, 0x0C0A1382, "jal 0x284e08             the only SetGameState(9)"),
    (0x002B54F0, 0x24050009, "addiu $a1, $zero, 9      ... that 9"),
    (0x002B555C, 0xAC550D9C, "sw $s5, 0xd9c($v0)       save block complete = 1"),

    (0x001A63B8, 0x8E630908, "lw $v1, 0x908($s3)       Gladiatoons: Taz pods"),
    (0x001A63BC, 0x8E62090C, "lw $v0, 0x90c($s3)       ... Daffy pods"),
    (0x001A63C0, 0x0043102A, "slt $v0, $v0, $v1        Daffy < Taz ?"),
    (0x001A63C4, 0x10400006, "beqz -> lose             a DRAW is a loss"),
    (0x001A98B8, 0x28420006, "slti $v0, $v0, 6         Disco: lose at >= 6"),
    (0x00198D24, 0x8062029C, "lb $v0, 0x29c($v1)       Zoo score is a BYTE"),
    (0x0018E80C, 0xAC430650, "sw $v1, 0x650($v0)       Dodge winner = 1"),
    (0x00184E44, 0x0C0B1136, "jal 0x2c44d8             the lone 0x5A install"),
    (0x00184E48, 0x2405005A, "addiu $a1, $zero, 0x5a   ... Hindenbird only"),
]


# ------------------------------------------------------------------- setup

def load_mem():
    """pcsx2_mem out of the world, so this uses the shipped one."""
    import types
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("pcsx2_mem",):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.pcsx2_mem"]


# ------------------------------------------------------------------- check

def cmd_check(_args):
    """Every address above, against the dump. No emulator needed."""
    if not os.path.exists(DUMP):
        print(f"  no {os.path.basename(DUMP)} -- take one with taz_ramdump.py")
        return 2
    with open(DUMP, "rb") as fh:
        d = fh.read()
    if len(d) < 0x02000000:
        print(f"  {os.path.basename(DUMP)} is {len(d)} bytes, expected 32MB")
        return 2

    bad = 0
    print(f"  {os.path.basename(DUMP)}: {len(d)} bytes, offset == EE address\n")
    for addr, want, what in EXPECT:
        got = struct.unpack_from("<I", d, addr)[0]
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} 0x{addr:08X}  {got:08X}"
              f"{'' if ok else f' (expected {want:08X})'}  {what}")

    # The module was identified by its source path, so assert that too --
    # it is the one claim that names the mechanism.
    end = d.find(b"\0", 0x0049A738)
    path = d[0x0049A738:end].decode("latin-1")
    want_path = "C:/Taz/Source/bossgamecontinue.cpp"
    ok = path == want_path
    bad += not ok
    print(f"\n  {'ok  ' if ok else 'BAD '} 0x0049A738  {path!r}"
          f"{'' if ok else f' (expected {want_path!r})'}")

    ptr = struct.unpack_from("<I", d, CONTINUE_PTR)[0]
    lid = d[LEVEL_BYTE]
    gs = struct.unpack_from("<I", d, GAME_STATE)[0]
    print(f"\n  0x{CONTINUE_PTR:08X}  continue ptr  = 0x{ptr:08X}")
    print(f"  0x{LEVEL_BYTE:08X}  level         = {lid}"
          f" ({BOSS_LEVELS.get(lid, 'not a boss level')})")
    print(f"  0x{GAME_STATE:08X}  game state    = {gs}")

    print()
    if bad:
        print(f"  {bad} of {len(EXPECT) + 1} did not match. These addresses "
              f"are for a different\n  build -- do not believe anything above.")
        return 1
    print(f"  all {len(EXPECT) + 1} match.")
    if ptr:
        print(f"  The continue pointer is NON-ZERO in this dump. Either it "
              f"was taken\n  during a Continue prompt, or the address is not "
              f"what this file says.")
    else:
        print("  The continue pointer is 0, which is the right idle value "
              "for a dump\n  taken during normal play. `watch` is what "
              "proves it moves.")
    return 0


# ------------------------------------------------------------------- watch

def sane(p):
    return PTR_LO <= p < PTR_HI


def sample(mem):
    out = {}
    p = mem.read_u32(CONTINUE_PTR)
    out["ptr"] = p
    out["answer"] = mem.read_u32(p + CONTINUE_ANSWER) if sane(p) else None
    out["phase"] = mem.read_u32(p + CONTINUE_PHASE) if sane(p) else None
    out["gstate"] = mem.read_u32(GAME_STATE)
    out["lvl"] = mem.read_u8(LEVEL_BYTE)
    try:
        st = mem.deref(TAZ_PTR, O_STATE_PTR, S_STATE)
        out["taz"] = mem.read_u8(st) if st else None
    except Exception:
        out["taz"] = None

    lid = out["lvl"]
    if lid == 7:
        out["ours"] = mem.read_u8(ZOO_OURS)
        out["theirs"] = mem.read_u8(ZOO_THEIRS)
        out["extra"] = mem.read_u32(ZOO_WINNER)
    elif lid == 12:
        out["ours"] = mem.read_u32(GLAD_OURS)
        out["theirs"] = mem.read_u32(GLAD_THEIRS)
        out["extra"] = round(mem.read_float(GLAD_CLOCK), 1)
    elif lid == 17:
        out["ours"] = out["theirs"] = None
        out["extra"] = mem.read_u32(WEST_WINNER)
    elif lid == 19:
        out["ours"] = mem.read_u32(DISCO_OURS)
        out["theirs"] = mem.read_u32(DISCO_THEIRS)
        out["extra"] = None
    else:
        out["ours"] = out["theirs"] = out["extra"] = None
    return out


def fmt(k, v):
    if v is None:
        return f"{k}=-"
    if k == "ptr":
        return f"ptr=0x{v:08X}"
    if k == "taz":
        return f"taz=0x{v:02X}"
    return f"{k}={v}"


ORDER = ["ptr", "answer", "phase", "gstate", "lvl", "taz",
         "ours", "theirs", "extra"]


def cmd_watch(args):
    mem = load_mem()
    if not mem.hook():
        print("  could not reach PCSX2. Is it running with the game booted, "
              "PINE on (Settings -> Advanced, slot 28011),\n  and the AP "
              "client CLOSED?")
        return 2
    print(f"  connected: {mem.game_id()}\n")
    print("  Watching 0x0037FC3C. Go into a boss and LOSE, then answer the\n"
          "  'Continue this game' prompt. Gladiatoons (12) is the one that\n"
          "  never worked, so it is the most useful single recording.\n")
    print("  What should happen, if the dump is right:\n"
          "    ptr goes 0 -> a real pointer at the losing blow\n"
          "    answer sits at 0 while the prompt is up\n"
          "    answer becomes 1 (continue) or 2 (quit) when you pick\n"
          "    ptr goes back to 0 shortly after\n")
    print("  Ctrl-C to stop.\n")

    t0 = time.time()
    prev = None
    up_since = None
    events = []
    last_answer = 0
    try:
        while time.time() - t0 < args.secs:
            now = sample(mem)
            if args.raw or prev is None or now != prev:
                t = time.time() - t0
                if prev is None:
                    line = "  ".join(fmt(k, now[k]) for k in ORDER)
                else:
                    line = "  ".join(fmt(k, now[k]) for k in ORDER
                                     if now[k] != prev[k]) or "(no change)"
                print(f"  {t:7.2f}s  {line}")

            p = now["ptr"]
            if sane(p) and up_since is None:
                up_since = time.time()
                lid = now["lvl"]
                print(f"  {time.time() - t0:7.2f}s  >>> BOSS LOSS -- level "
                      f"{lid} ({BOSS_LEVELS.get(lid, '?')}), "
                      f"ptr 0x{p:08X}")
                last_answer = 0
            elif sane(p) and now["answer"] and now["answer"] != last_answer:
                last_answer = now["answer"]
                word = {1: "CONTINUE", 2: "QUIT"}.get(last_answer, "?")
                print(f"  {time.time() - t0:7.2f}s      answered "
                      f"{last_answer} ({word})")
            elif not sane(p) and up_since is not None:
                dur = time.time() - up_since
                events.append((now["lvl"], dur, last_answer))
                print(f"  {time.time() - t0:7.2f}s  <<< cleared after "
                      f"{dur:.2f}s")
                up_since = None

            if now["gstate"] == 9 and (prev or {}).get("gstate") != 9:
                print(f"  {time.time() - t0:7.2f}s  *** GAME_STATE 9 -- "
                      f"level completed (the shared WIN)")

            prev = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  stopped.")

    print()
    if events:
        print(f"  {len(events)} boss loss(es) recorded:")
        for lid, dur, ans in events:
            print(f"    level {lid:<3} {BOSS_LEVELS.get(lid, '?'):<16} "
                  f"up for {dur:6.2f}s, answered {ans}")
        shortest = min(d for _, d, _ in events)
        print(f"\n  Shortest window {shortest:.2f}s. The client polls at "
              f"0.1s, so anything\n  over ~0.3s is comfortably detectable.")
    else:
        print("  No boss loss seen. If you DID lose one, then 0x0037FC3C is "
              "not the\n  signal on this build and that is the finding -- "
              "say so, and say which\n  boss, because it changes what to "
              "look at next.")
    mem.un_hook()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="verify the addresses against ee_dump.bin")
    c.set_defaults(fn=cmd_check)

    w = sub.add_parser("watch", help="record a real boss loss")
    w.add_argument("--secs", type=float, default=900.0)
    w.add_argument("--interval", type=float, default=0.05)
    w.add_argument("--raw", action="store_true",
                   help="print every sample, not just changes")
    w.set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
