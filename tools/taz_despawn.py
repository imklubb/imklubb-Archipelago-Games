#!/usr/bin/env python3
"""Working out how to make a keeper leave.

Writing 0xE to the animation field does not do it. That was a guess from the
name in the notes, and the run that tested it disproved it twice over: the
keeper stayed, and ANOTHER keeper was already sitting at anim 14 in the same
level, loaded and undefeated. So 0xE is a state keepers pass through, not an
instruction to go.

    py -3.13 taz_despawn.py list                   what is loaded, right now
    py -3.13 taz_despawn.py watch                  RECORD a real takedown
    py -3.13 taz_despawn.py watch --idx 2          just that one
    py -3.13 taz_despawn.py try --idx 2            work down the candidates
    py -3.13 taz_despawn.py try --idx 2 --only fadeflag
    py -3.13 taz_despawn.py poke --idx 2 --sub --off 0xCC --val 1
    py -3.13 taz_despawn.py all                    vanish every keeper, and
                                                   keep vanishing them
    py -3.13 taz_despawn.py all --only 0,2         just those two

`watch` is the one that matters. It samples a keeper's whole object and its
sub-object several times a second and prints every field that CHANGES, with a
timestamp, until it leaves the array. Beat a keeper for real while it runs and
the recording says exactly what the game does to remove one -- which is the
thing to copy. Nothing is written; it is safe on a real seed.

`try` writes. It works through candidate recipes one at a time on a keeper you
name, waiting to see whether it leaves, and puts every field back if it does
not. Use it in a level you do not mind.

`all` is the one to run to see whether they STAY gone. It vanishes every
keeper it can see and keeps watching, so keepers that stream in later as Taz
moves through the level get sent away too, and it reports anything that comes
back. That is exactly what the client does with banked catchers -- this just
does it to all of them so a level can be walked end to end in one go.

Close the AP client first -- PINE takes one connection at a time.
"""

import argparse
import importlib.util
import json
import os
import struct
import sys
import time

# The repo root, not this folder -- see tools/taz_paths.py. Every
# os.path.join(HERE, ...) below still means what it always did.
from taz_paths import ROOT as HERE, TOOLS    # noqa: E402
WORLD = os.path.join(HERE, "worlds", "tazwanted")

OBJ_BYTES = 0x200        # the enemy object
SUB_BYTES = 0x200        # its state sub-object


def load_game():
    """The world's game.py, loaded under a synthetic package. Same trick as
    taz_catcher_test.py, and for the same reason: this reads the shipped
    layout constants rather than a re-typed copy of them."""
    import types
    pkg = types.ModuleType("tazworld")
    pkg.__path__ = [WORLD]
    sys.modules["tazworld"] = pkg
    for name in ("_imports", "logic", "game"):
        path = os.path.join(WORLD, name + ".py")
        spec = importlib.util.spec_from_file_location("tazworld." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tazworld." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
    return sys.modules["tazworld.game"]


def hooked(G):
    mem = sys.modules["tazworld.game"].mem
    if mem is None:
        print("    pcsx2_mem did not import, so there is nothing to hook.")
        return None, None
    game = G.Game()
    try:
        ok = game.connect()
    except Exception as e:
        print(f"    hooking PCSX2 failed: {type(e).__name__}: {e}")
        return None, None
    if not ok:
        print("    could not reach PCSX2 on PINE. Is the game running, and is "
              "PINE enabled in Settings -> Advanced? Close the AP client too.")
        return None, None
    path = os.path.join(WORLD, "data", "taz_catchers.json")
    game.start_catchers(json.load(open(path, encoding="utf-8")))
    return game, mem


def keepers(G, mem):
    try:
        return G.TazPS2(mem).catchers()
    except Exception:
        return []


def label(game, G, k):
    lid = game.level_id()
    idx = game._catchers.match_post(lid, k.get("pos"))
    return idx


def pick(game, G, mem, idx):
    """The loaded keeper matching post `idx`, or None."""
    lid = game.level_id()
    for k in keepers(G, mem):
        if game._catchers.match_post(lid, k.get("pos")) == idx:
            return k
    return None


# ---------------------------------------------------------------- list

def cmd_list(args):
    G = load_game()
    game, mem = hooked(G)
    if game is None:
        return 1
    lid = game.level_id()
    ks = keepers(G, mem)
    print(f"    level {lid}, enemy total {game._read_enemy_total()}, "
          f"{len(ks)} keeper(s) loaded")
    posts = game._catcher_posts.get(lid) or []
    for k in ks:
        i = game._catchers.match_post(lid, k.get("pos"))
        d = (G.dist2(k["pos"], posts[i]) ** 0.5
             if i is not None and i < len(posts) else None)
        sub = mem.read_u32(k["ptr"] + G.E_SUB)
        print(f"      catcher {str(i):>4s}  obj {k['ptr']:08X}  "
              f"sub {sub:08X}  anim {k['anim']:3d}  "
              f"defeated {int(bool(k['defeated']))}  "
              f"{('%8.0f from post' % d) if d is not None else 'no post match'}")
    return 0


# ---------------------------------------------------------------- watch

def snap(mem, ptr, sub):
    try:
        return (mem.read_bytes(ptr, OBJ_BYTES),
                mem.read_bytes(sub, SUB_BYTES))
    except Exception:
        return None


def diff(a, b, base_name):
    """Changed words between two snapshots, as (offset, before, after)."""
    out = []
    for off in range(0, min(len(a), len(b)), 4):
        x = struct.unpack_from("<I", a, off)[0]
        y = struct.unpack_from("<I", b, off)[0]
        if x != y:
            out.append((base_name, off, x, y))
    return out


def as_float(v):
    f = struct.unpack("<f", struct.pack("<I", v))[0]
    if f == f and abs(f) < 1e9 and (abs(f) > 1e-6 or f == 0.0):
        return f
    return None


NAMED = {}


def describe(G, which, off):
    if not NAMED:
        NAMED.update({
            ("obj", G.E_TYPE): "E_TYPE",
            ("obj", G.E_POS): "E_POS x",
            ("obj", G.E_POS + 4): "E_POS y",
            ("obj", G.E_POS + 8): "E_POS z",
            ("obj", G.E_SUB): "E_SUB",
            ("sub", G.E_ANIM): "E_ANIM",
            ("sub", G.E_DEFEATED): "E_DEFEATED",
        })
    return NAMED.get((which, off), "")


def cmd_watch(args):
    G = load_game()
    game, mem = hooked(G)
    if game is None:
        return 1
    lid = game.level_id()

    k = None
    while k is None:
        ks = keepers(G, mem)
        if args.idx is None:
            k = ks[0] if ks else None
        else:
            k = pick(game, G, mem, args.idx)
        if k is None:
            print("    waiting for a keeper to load... (Ctrl-C to stop)")
            time.sleep(1.0)
            if game.level_id() != lid:
                lid = game.level_id()
    ptr = k["ptr"]
    sub = mem.read_u32(ptr + G.E_SUB)
    idx = game._catchers.match_post(lid, k.get("pos"))
    print(f"    watching catcher {idx} in level {lid}: "
          f"obj {ptr:08X}, sub {sub:08X}")
    print(f"    {OBJ_BYTES} bytes of each, {1 / args.step:.0f} times a second."
          " Read-only.")
    print("    Now go and beat it. Ctrl-C to stop.")
    print()

    prev = snap(mem, ptr, sub)
    if prev is None:
        print("    could not read it.")
        return 1
    t0 = time.time()
    log = []
    quiet = {("obj", G.E_POS), ("obj", G.E_POS + 4), ("obj", G.E_POS + 8)}
    try:
        while True:
            time.sleep(args.step)
            live = {x["ptr"] for x in keepers(G, mem)}
            total = game._read_enemy_total()
            if ptr not in live:
                t = time.time() - t0
                print(f"      [{t:6.2f}] LEFT THE ARRAY. enemy total {total}")
                log.append({"t": t, "event": "left", "total": total})
                break
            cur = snap(mem, ptr, sub)
            if cur is None:
                continue
            changes = (diff(prev[0], cur[0], "obj")
                       + diff(prev[1], cur[1], "sub"))
            prev = cur
            t = time.time() - t0
            for which, off, x, y in changes:
                if not args.all and (which, off) in quiet:
                    continue
                name = describe(G, which, off)
                fx, fy = as_float(x), as_float(y)
                extra = ""
                if fx is not None and fy is not None and (fx or fy):
                    extra = f"   ({fx:.2f} -> {fy:.2f})"
                print(f"      [{t:6.2f}] {which}+0x{off:03X} "
                      f"{name:12s} {x:08X} -> {y:08X}{extra}")
                log.append({"t": t, "where": which, "off": off,
                            "from": x, "to": y, "name": name})
    except KeyboardInterrupt:
        print()

    out = os.path.join(TOOLS, "taz_despawn_record.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"level": lid, "catcher": idx, "obj": ptr, "sub": sub,
                   "log": log}, f, indent=2)
    print(f"    {len(log)} change(s) written to {out}")
    return 0


# ---------------------------------------------------------------- try

# What a real takedown does, from taz_despawn_record.json -- catcher 1 in
# Zooney Tunes, watched from before the fight to after it vanished.
#
#   t+24.02  THE TAKEDOWN, all in one frame:
#              sub+0xB0  E_ANIM        2 -> 6      (6 is "defeated")
#              sub+0xB4                2 -> 6      moves WITH 0xB0, always
#              sub+0xCC  E_DEFEATED    0 -> 1
#              sub+0xD8                0x003640C0 -> 0x003645B8   anim data
#              sub+0xDC                20 -> 3     a frame counter, restarted
#   t+24.66    obj+0x1F8               0x1000 -> 0                a flag drops
#   t+26.90  THE VANISH, 2.9s later, again all in one frame:
#              sub+0xB0  E_ANIM        6 -> 14     <- 14 is the RESULT
#              obj+0x1F8               0 -> 0x20   <- a flag is SET
#              obj+0x1D4               0x00685840 -> 0           pointer cleared
#              obj+0x1F0               0.0 -> -128.0             a rate?
#              obj+0x1E4               0x01000100 -> 0x010000EB
#              obj+0x014, 0xD0, 0xD4, 0xD8   1.000 -> 1.028      scale, rising
#   t+26.9..27.6  obj+0x1E4's low half falls 256 -> 235 -> 208 -> 181 -> 154
#                 -> 118: an ALPHA fade. The scale swells 1.03 then collapses
#                 to 0.078.
#   t+27.71  gone from the array, enemy total 12 -> 11.
#
# Two things follow from this. First, anim 14 is the RESULT of the vanish, not
# its cause -- which is why writing it did nothing, and why another keeper was
# sitting at 14 while still loaded. Second, obj+0x1F8 is the same offset as
# Taz's O_ACTOR_FLAGS, where SQUASH_BIT is 0x40. These objects share an actor
# base, so 0x1F8 is a known-live flags word and bit 0x20 goes up at the exact
# frame the fade starts. That is the best single candidate here.


def RECIPE(G):
    """The confirmed recipe, read from the world rather than re-typed here, so
    a change in one place cannot leave the other saying something else."""
    return list(G.DESPAWN_RECIPE or [])


# Variants for `all --variant X`, to find out how much of the delay is the
# fade and how much is the game simply not having loaded the keeper yet.
#
# From the recording, obj+0x1E4 is 0x01000100 and its LOW half counts down --
# 256, 235, 208, 181, 154, 118 -- while obj+0x1F0 holds -128.0. So the low
# half is the fade and 0x1F0 is its rate. Two ways to shorten it: a steeper
# rate, or setting the fade straight to zero. If the game removes the keeper
# when that value hits zero, `alpha0` should be close to instant -- which is a
# guess, and the reason this verb reports times instead of my opinion.
FADE_WORD = 0x1E4
FADE_RATE = 0x1F0


def VARIANTS(G):
    """Spelled out rather than derived from the shipped recipe, so these names
    keep meaning the same thing after the shipped one changes."""
    core = [("obj", 0x1F8, "|", 0x20),
            ("sub", G.E_ANIM, "=", G.ANIM_DESPAWN),
            ("sub", G.E_ANIM + 4, "=", G.ANIM_DESPAWN)]
    slow = ("obj", FADE_RATE, "=", 0xC3000000)      # -128.0, the game's own
    fast = ("obj", FADE_RATE, "=", 0xC5800000)      # -4096.0
    zero = ("obj", FADE_WORD, "=", 0x01000000)
    return {
        "shipped": RECIPE(G),
        "base":    core + [slow],
        "alpha0":  core + [slow, zero],
        "fast":    core + [fast],
        "both":    core + [fast, zero],
    }


def CANDIDATES(G):
    """Recipes to try, most likely first.

    Each write is (where, offset, op, value); `where` is "obj" or "sub" and
    `op` is "=" for a plain write or "|" for setting bits. They come from the
    recording above rather than from names in the notes, which is what went
    wrong the first time.
    """
    return [
        ("defeat",
         "everything the game changed at the moment of the takedown, so its "
         "own update runs the rest of the sequence",
         [("sub", G.E_ANIM, "=", G.ANIM_DEFEATED),
          ("sub", G.E_ANIM + 4, "=", G.ANIM_DEFEATED),
          ("sub", G.E_DEFEATED, "=", 1)]),

        ("fadeflag",
         "obj+0x1F8 bit 0x20, which goes up on the exact frame the keeper "
         "starts to fade. Same flags word as Taz's squash bit",
         [("obj", 0x1F8, "|", 0x20)]),

        ("defeat+fade",
         "both, in case the flag is only read while it is defeated",
         [("sub", G.E_ANIM, "=", G.ANIM_DEFEATED),
          ("sub", G.E_ANIM + 4, "=", G.ANIM_DEFEATED),
          ("sub", G.E_DEFEATED, "=", 1),
          ("obj", 0x1F8, "|", 0x20)]),

        ("vanish",
         "the whole vanish frame copied out: the flag, the rate, and anim 14",
         [("obj", 0x1F8, "|", 0x20),
          ("obj", 0x1F0, "=", 0xC3000000),        # -128.0
          ("sub", G.E_ANIM, "=", G.ANIM_DESPAWN),
          ("sub", G.E_ANIM + 4, "=", G.ANIM_DESPAWN)]),

        ("defeated",
         "the flag on its own -- cheap, and worth knowing either way",
         [("sub", G.E_DEFEATED, "=", 1)]),

        ("alpha",
         "not a despawn at all: force the fade's alpha to zero. A last "
         "resort, and a bad one -- an invisible keeper can still catch Taz",
         [("obj", 0x1E4, "=", 0x01000000)]),
    ]


def apply(mem, base, w):
    """One write. Returns what was there before, for putting back."""
    where, off, op, value = w
    a = base[where] + off
    before = mem.read_u32(a)
    mem.write_u32(a, (before | value) if op == "|" else value)
    return before


def shown(w):
    where, off, op, value = w
    return f"{where}+0x{off:X}{op}{value:#x}"


def cmd_try(args):
    G = load_game()
    game, mem = hooked(G)
    if game is None:
        return 1
    lid = game.level_id()

    recipes = CANDIDATES(G)
    if args.only:
        recipes = [r for r in recipes if r[0] == args.only]
        if not recipes:
            print(f"    no candidate called {args.only!r}. Known: "
                  + ", ".join(r[0] for r in CANDIDATES(G)))
            return 1

    print(f"    level {lid}. THIS WRITES. Each candidate gets "
          f"{args.wait:g}s to work, and is undone if it does not.")
    print()
    for name, why, writes in recipes:
        k = pick(game, G, mem, args.idx)
        if k is None:
            print(f"    catcher {args.idx} is not loaded -- walk nearer, or "
                  "pick another with `list`.")
            return 1
        ptr = k["ptr"]
        sub = mem.read_u32(ptr + G.E_SUB)
        if not mem.valid_ptr(sub):
            print("    its sub-object did not read back sensibly.")
            return 1

        base = {"obj": ptr, "sub": sub}
        before = [(w, apply(mem, base, w)) for w in writes]
        total0 = game._read_enemy_total()
        print(f"    {name:14s} {' '.join(shown(w) for w in writes)}")
        print(f"        {why}")

        gone, t0 = False, time.time()
        while time.time() - t0 < args.wait:
            if ptr not in {x["ptr"] for x in keepers(G, mem)}:
                gone = True
                break
            time.sleep(0.15)
        if gone:
            print(f"        LEFT after {time.time() - t0:.1f}s. "
                  f"enemy total {total0} -> {game._read_enemy_total()}")
            print()
            print(f"    That is the one: {name}")
            return 0
        now = " ".join(f"{w[0]}+0x{w[1]:X}={mem.read_u32(base[w[0]] + w[1]):#x}"
                       for w in writes)
        print(f"        still there. reads back {now}")
        for w, v in before:
            try:
                mem.write_u32(base[w[0]] + w[1], v)
            except Exception:
                pass
        print("        put back")
        print()
    print("    none of them worked. Run `watch` through a real takedown and "
          "send me the recording -- that says what the game actually does.")
    return 1


# ---------------------------------------------------------------- poke

def cmd_all(args):
    """Vanish every keeper, and keep doing it as more stream in.

    A keeper is only in the array while Taz is near enough, so one pass sends
    away whatever is loaded at that moment and nothing else. Walking the level
    with this running is what shows whether they stay gone -- and re-entering
    the level is what shows they come back, which is why the client clears its
    record on a level change and vanishes them again.
    """
    G = load_game()
    game, mem = hooked(G)
    if game is None:
        return 1
    want = None
    if args.only:
        want = {int(x) for x in args.only.replace(",", " ").split()}
    recipe = VARIANTS(G).get(args.variant)
    if not recipe:
        print(f"    no variant called {args.variant!r}. Known: "
              + ", ".join(VARIANTS(G)))
        return 1

    lid = game.level_id()
    print(f"    level {lid}. THIS WRITES, to every keeper"
          + (f" matching {sorted(want)}" if want else "") + ".")
    print(f"    variant {args.variant}: "
          + " ".join(shown(w) for w in recipe))
    print("    Walk the level. Ctrl-C to stop.")
    print()

    done, seen, back, t0 = set(), {}, 0, time.time()
    waiting, times = {}, []
    try:
        while True:
            here = game.level_id()
            if here != lid:
                print(f"      [{time.time() - t0:6.1f}] level {lid} -> {here}, "
                      f"starting over ({len(done)} sent away)")
                lid, done, seen, waiting = here, set(), {}, {}
            live = {k["ptr"] for k in keepers(G, mem)}
            for ptr in [p for p in waiting if p not in live]:
                dt = time.time() - waiting.pop(ptr)
                times.append(dt)
                print(f"      [{time.time() - t0:6.1f}] "
                      f"  ... gone {dt:.2f}s after the write")
            for k in keepers(G, mem):
                ptr = k["ptr"]
                if ptr in done:
                    continue
                idx = game._catchers.match_post(lid, k.get("pos"))
                if want is not None and idx not in want:
                    continue
                sub = mem.read_u32(ptr + G.E_SUB)
                if not mem.valid_ptr(sub):
                    continue
                base = {"obj": ptr, "sub": sub}
                for w in recipe:
                    apply(mem, base, w)
                waiting[ptr] = time.time()
                done.add(ptr)
                seen[idx] = seen.get(idx, 0) + 1
                if seen[idx] > 1:
                    back += 1
                    print(f"      [{time.time() - t0:6.1f}] catcher {idx} is "
                          f"BACK as {ptr:08X} -- sent away again "
                          f"(time {seen[idx]})")
                else:
                    print(f"      [{time.time() - t0:6.1f}] catcher {idx} "
                          f"({ptr:08X}) sent away. total "
                          f"{game._read_enemy_total()}")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print()
    print(f"    stopped. {len(done)} keeper(s) sent away"
          + (f", {back} of them a second time" if back else ""))
    if times:
        print(f"    write -> gone: {min(times):.2f}s best, "
              f"{sum(times) / len(times):.2f}s average, "
              f"{max(times):.2f}s worst, over {len(times)}")
        if min(times) <= 0.25:
            print("    0.20s is this tool's own poll interval, so that is the "
                  "floor -- it cannot tell 0.2s from 0.02s.")
        print("    Anything still slow after that is the game not having "
              "loaded the keeper yet, which nothing here can hurry.")
    if back:
        print("    A catcher coming back as a NEW pointer means the game "
              "respawned it, not that the write failed.")
    return 0


def cmd_poke(args):
    G = load_game()
    game, mem = hooked(G)
    if game is None:
        return 1
    k = pick(game, G, mem, args.idx)
    if k is None:
        print(f"    catcher {args.idx} is not loaded.")
        return 1
    base = mem.read_u32(k["ptr"] + G.E_SUB) if args.sub else k["ptr"]
    a = base + args.off
    before = mem.read_u32(a)
    mem.write_u32(a, args.val)
    print(f"    {a:08X}: {before} -> {args.val}")
    t0 = time.time()
    while time.time() - t0 < args.wait:
        if k["ptr"] not in {x["ptr"] for x in keepers(G, mem)}:
            print(f"    LEFT after {time.time() - t0:.1f}s")
            return 0
        time.sleep(0.15)
    print(f"    still there; the field reads {mem.read_u32(a)}")
    if not args.keep:
        mem.write_u32(a, before)
        print("    put back")
    return 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    a = sub.add_parser("all")
    a.add_argument("--only", help="only these catchers, e.g. 0,2")
    a.add_argument("--variant", default="shipped",
                   help="shipped, base, alpha0, fast, or both")
    a.set_defaults(fn=cmd_all)

    w = sub.add_parser("watch")
    w.add_argument("--idx", type=int, help="which catcher, by post number")
    w.add_argument("--step", type=float, default=0.15)
    w.add_argument("--all", action="store_true",
                   help="include position, which changes constantly")
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("try")
    t.add_argument("--idx", type=int, required=True)
    t.add_argument("--only", help="just this candidate")
    t.add_argument("--wait", type=float, default=6.0)
    t.set_defaults(fn=cmd_try)

    p = sub.add_parser("poke")
    p.add_argument("--idx", type=int, required=True)
    p.add_argument("--off", type=lambda s: int(s, 0), required=True)
    p.add_argument("--val", type=lambda s: int(s, 0), required=True)
    p.add_argument("--sub", action="store_true", help="offset is in E_SUB")
    p.add_argument("--wait", type=float, default=6.0)
    p.add_argument("--keep", action="store_true", help="do not undo it")
    p.set_defaults(fn=cmd_poke)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
