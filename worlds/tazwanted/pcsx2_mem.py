#!/usr/bin/env python3
"""
pcsx2_mem.py -- PCSX2 memory access with a dolphin_memory_engine-shaped API.

Drop-in so the Dolphin tooling ports with one import change:

    import dolphin_memory_engine as dme     ->    import pcsx2_mem as dme

Requires pine.py next to this file. Copy it out of the Sly 2 apworld:

    sly2.apworld/sly2/pcsx2_interface/pine.py

PCSX2 setup:
    Settings -> Advanced -> enable PINE, slot 28011 (search settings for
    "PINE" if it has moved). PCSX2 must be running with the game booted.

TWO THINGS THAT WILL BITE YOU COMING FROM GAMECUBE
--------------------------------------------------
1. PS2 is LITTLE-endian. GameCube was big-endian. Every struct format flips:
   ">I" becomes "<I". A value of 1000 that read as `00 00 03 E8` on GameCube
   reads as `E8 03 00 00` here. read_u32 below handles it; raw byte dumps
   will look reversed until you get used to it.

2. Pine.read_bytes moves 8 bytes per socket round trip -- reading a 960-byte
   region that way is 120 round trips. read_bytes below uses batch_read_int32
   instead, which fetches many words per request.

Addresses: RetroAchievements PS2 notes map directly onto EE memory, so a note
address like 0x3FF060 can be passed straight through.
"""

import struct
import sys

# pine lives in pcsx2_interface/ inside the apworld and beside this file when
# the tools are run on their own, so both are tried. Getting this wrong is
# quiet and confusing: the import fails, `mem` ends up None, and the client
# never connects without ever saying why.
Pine = None
_PINE_ERROR = None
for _attempt in ("relative", "sibling", "path"):
    try:
        if _attempt == "relative":
            from .pcsx2_interface.pine import Pine
        elif _attempt == "sibling":
            from pcsx2_interface.pine import Pine
        else:
            from pine import Pine
        break
    except Exception as _e:
        _PINE_ERROR = _e
        Pine = None

if Pine is None:
    raise ImportError(
        "pine.py could not be imported. It belongs in "
        "pcsx2_interface/pine.py inside the world, or beside this file when "
        f"running the tools directly. Last error: {_PINE_ERROR}")

# PS2 EE main RAM is 32MB. Pointers outside this are garbage -- which is what
# you get during loads and menus, when the object simply doesn't exist.
EE_MIN, EE_MAX = 0x00100000, 0x02000000

_pine = None
_WORDS_PER_REQUEST = 192          # keep each batched request modest


# ---------------------------------------------------------------- connect


def hook(slot=28011):
    global _pine
    if _pine is None:
        _pine = Pine(slot)
    _pine.connect()
    return _pine.is_connected()


def is_hooked():
    return _pine is not None and _pine.is_connected()


def un_hook():
    global _pine
    if _pine is not None:
        try:
            _pine.disconnect()
        except Exception:
            pass
    _pine = None


def game_id():
    """PS2 serial, e.g. SLUS-xxxxx. Use it to verify the right disc."""
    return _pine.get_game_id()


# ---------------------------------------------------------------- reads


def read_bytes(address, size):
    """Raw bytes in address order, batched for speed."""
    if size <= 0:
        return b""
    start = address & ~3
    end = (address + size + 3) & ~3
    addrs = list(range(start, end, 4))

    out = bytearray()
    for i in range(0, len(addrs), _WORDS_PER_REQUEST):
        chunk = addrs[i:i + _WORDS_PER_REQUEST]
        for v in _pine.batch_read_int32(chunk):
            out += (v & 0xFFFFFFFF).to_bytes(4, "little")

    off = address - start
    return bytes(out[off:off + size])


def read_u8(address):
    return _pine.read_int8(address)


def read_u16(address):
    return _pine.read_int16(address)


def read_u32(address):
    return _pine.read_int32(address)


def read_float(address):
    return struct.unpack("<f", read_bytes(address, 4))[0]


def read_floats(address, count):
    raw = read_bytes(address, 4 * count)
    return struct.unpack("<" + "f" * count, raw)


def write_u8(address, value):
    _pine.write_int8(address, value & 0xFF)


def write_u16(address, value):
    _pine.write_int16(address, value & 0xFFFF)


def write_u32(address, value):
    _pine.write_int32(address, value & 0xFFFFFFFF)


def write_float(address, value):
    write_bytes(address, struct.pack("<f", value))


def write_floats(address, values):
    write_bytes(address, struct.pack("<" + "f" * len(values), *values))


def write_bytes(address, data):
    _pine.write_bytes(address, data)


# ---------------------------------------------------------------- pointers


def valid_ptr(p):
    return p is not None and EE_MIN <= p < EE_MAX


def follow(address, *offsets):
    """Chase a pointer chain, returning None the moment it goes bad.

    RA notes are written as chains -- Taz Pointer -> +0x1cc -> +0x11c -- so
    almost every read needs this. Returns None during loads instead of
    raising, so a polling client needs no special-casing.

        costume_addr = follow(0x3FF060, 0x1CC)
        if costume_addr: value = read_u8(costume_addr + 0x11C)
    """
    try:
        p = read_u32(address)
    except Exception:
        return None
    if not valid_ptr(p):
        return None
    for off in offsets:
        try:
            nxt = read_u32(p + off)
        except Exception:
            return None
        if not valid_ptr(nxt):
            return None
        p = nxt
    return p


def deref(address, *offsets):
    """Like follow(), but the LAST offset is a field, not a pointer.

        deref(0x3FF060, 0x1CC, 0x11C)   -> address of the costume byte
    """
    if not offsets:
        return follow(address)
    base = follow(address, *offsets[:-1])
    return None if base is None else base + offsets[-1]


# ---------------------------------------------------------------- self test


def _selftest():
    print("connecting to PCSX2 via PINE...")
    if not hook():
        print("  FAILED. Check that PCSX2 is running with a game booted,")
        print("  and that PINE is enabled in Settings -> Advanced (slot 28011).")
        return 1
    print("  connected")

    try:
        print(f"  game id: {game_id()}")
    except Exception as e:
        print(f"  could not read game id: {e}")

    taz = read_u32(0x3FF060)
    print(f"\n  [0x003FF060] Taz pointer = 0x{taz:08X}  "
          f"{'valid' if valid_ptr(taz) else 'null/invalid (in a menu or loading?)'}")

    if valid_ptr(taz):
        for name, chain in (("costume obj", (0x1CC,)),
                            ("state obj",   (0x1C0,)),
                            ("bonus obj",   (0x1C8,))):
            p = follow(0x3FF060, *chain)
            print(f"    {name:<12} = "
                  + (f"0x{p:08X}" if p else "unavailable"))

        c = deref(0x3FF060, 0x1CC, 0x11C)
        if c:
            v = read_u8(c)
            print(f"\n    costume byte @0x{c:08X} = 0x{v:02X}"
                  f"  {'(none)' if v == 0xFF else ''}")

        x = deref(0x3FF060, 0xC0)
        if x:
            print(f"    Taz position = "
                  f"({read_float(x):.1f}, "
                  f"{read_float(x + 4):.1f}, "
                  f"{read_float(x + 8):.1f})")
            print("\n  If the position changes as you move, everything works.")

    un_hook()
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
