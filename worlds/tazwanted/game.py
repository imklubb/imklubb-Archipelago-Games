"""
game.py -- the memory layout, the on-screen text, and every read and write.

Everything that touches the running game, in one place. The client above knows
about Archipelago and nothing about addresses; this knows about addresses and
nothing about Archipelago.

Imports pcsx2_mem defensively: it calls sys.exit when pine is missing, and a
generation server has no emulator. Every read and write checks for that first,
so the world still loads and only the live parts are unavailable.
"""

import sys as _sys

from . import _imports
from . import logic as D

# notify.py owns the trampoline, which is the only way anything here can make
# the game call one of its OWN functions -- see the bounty award. Imported
# defensively: several of the research tools load game.py on its own, and a
# missing trampoline has to mean "no banner", never "no client".
try:
    from . import notify as N
except Exception:                       # pragma: no cover
    N = None

mem = _imports.optional("pcsx2_mem")

# The save format lives in logic, because generation needs it without an
# emulator; the live addresses -- LEVEL_ID, TAZ_PTR and the rest -- stay here.
# T has to reach both, so it resolves against this module first and falls back
# to logic.
#
# Getting this wrong was quiet and expensive: T was simply logic, so every
# T.LEVEL_ID raised AttributeError, the caller caught it and returned None, and
# the client ran perfectly while reading nothing at all.
class _Layout:
    """Names from this module, then from logic."""

    def __getattr__(self, name):
        mod = _sys.modules[__name__]
        try:
            return mod.__dict__[name]
        except KeyError:
            pass
        try:
            return getattr(D, name)
        except AttributeError:
            raise AttributeError(
                f"{name} is in neither game.py nor logic.py") from None


T = _Layout()

# Names that moved to logic.py in the merge but are still referenced bare in
# this module's functions. They have to be real globals here: Python resolves a
# bare name against module globals and then builtins, and never consults a
# module-level __getattr__, so a fallback there does not help.
HUBS = D.HUBS
SANDWICH_GOAL = D.SANDWICH_GOAL

# What the spoof writes instead of 100. Read the long comment on
# sandwich_tick before changing it -- the difference between 100 and 101 is
# the difference between a level keeping its sandwiches and losing them:
#
#   0x0021C9EC   slti v1,v1,0x64   the bonus game portal wants >= 100
#   0x0024A6DC   bnel v1,t4        the sandwich destroyer wants == 100
#
# so 101 satisfies the first and never matches the second. It is also a
# number the game itself cannot produce, which makes it self-identifying.
SPOOF_COUNT = SANDWICH_GOAL + 1


def __getattr__(name):
    """Fall back to logic for any name this module does not define.

    When the nine original modules were merged into three, the save-format
    constants moved to logic.py and some bare references to them stayed here.
    Each one raised NameError the first time its line ran -- which for
    enforce_access was only once a save file existed, so it looked like a
    connection problem rather than a missing name.

    Rather than chase them individually, anything missing here is looked up in
    logic. Python calls this only when normal lookup has already failed, so it
    costs nothing when everything resolves.
    """
    try:
        return getattr(D, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}, "
            f"and neither does logic") from None


# ==========================================================================
# MEMORY LAYOUT
# ==========================================================================

# ---------------------------------------------------------------- globals

GAME_STATE      = 0x3FF040
LEVEL_ID        = 0x3FF048
PRIOR_STATE     = 0x3FF050
LEVEL_SECONDS   = 0x3FF058
TAZ_PTR         = 0x3FF060
DIFFICULTY      = 0x3FF2E4
CURRENT_FILE    = 0x3FF2F0
CURRENT_BOUNTY  = 0x507210
CURRENT_SANDWICH = 0x507214
TOTAL_BOUNTY_LIVE = 0x3CA3A8
TOTAL_BOUNTY_SAVE = 0x40403C
GALLERY_BASE    = 0x3FFD74          # 10 bitflags, 4 bytes apart

# Setting this to 1 opens EVERY warp door in the game, independently of boss
# progress. It is session state, not save data, so the client must re-assert
# it rather than write it once.
#
# Caveat: it also opens the two final boss doors (Disco Volcano and The
# Hindenbird), which have no other gate -- those need geofencing.
WARP_DOORS_OPEN = 0x3FBE44

# The title screen starts a demo if left idle. During it the level id becomes a
# real level and the game plays itself -- collecting posters, eating sandwiches,
# reaching destruction thresholds. So the client must send no checks and write
# no save data while this is set, or an idle menu quietly completes locations.
DEMO_MODE = 0x412600

# THERE IS NO "LIVE BLOCK". This used to say there was one at 0x00408BC4,
# holding what the current run had managed as opposed to the save block's
# best-ever, found by searching two levels for the destruction percentage --
# Ice Burg at 0x00408E00, Zooney Tunes at 0x00409038, one stride apart.
#
# Both of those readings were correct. The conclusion drawn from them was not.
# Work the address back through the save geometry:
#
#     level_block(3, slot 2) + L_TOTAL_BOUNTY
#       = 0x00400444 + 2*0x42B4 + 0*0x238 + 0x218
#       = 0x00408BC4
#
# to the byte. It was never a live block -- it is SAVE SLOT 2's per-level
# bounty, and the dump it was found in was taken on slot 2 (0x003FF2F0 reads
# 2), so the numbers looked current because they WERE current.
#
# The cost of the misreading: `live_block` added `save_file * 0x1000`, and the
# real slot stride is 0x42B4, so the Raised Bounty item wrote a correct-looking
# address on no save file at all. On file 0 it raised slot 2's bounty for the
# level; on files 1 and 2 it landed on an unaligned offset belonging to no
# field. The player saw nothing either way, which is exactly how it was
# reported.
#
# `level_block` in logic.py was right the whole time. Use it.
#
#     the level's bounty  =  level_block(lid, file) + D.L_TOTAL_BOUNTY   (+0x218)
#     the running total   =  TOTAL_BOUNTY_SAVE + file * D.FILE_STRIDE

# The game's own bounty award, and the animated banner that comes with it.
#
#     0x00201DD0(a0 = string index, a1 = dollars)
#
# It is not a display function -- it is the crediting one. It adds a1 to the
# level's bounty at 0x00201E8C, reads the save slot as a SIGNED BYTE at
# 0x00201E90, and sets BOUNTY_TARGET at 0x00201EA8 to the running total plus
# a1; the per-frame driver at 0x00202140 then walks the real total up to that
# target and runs the slow-motion. Every one of the game's own awards goes
# through it and none of them adds anything itself, which is what makes
# calling it the whole fix rather than half of one.
BOUNTY_POPUP  = 0x00201DD0
BOUNTY_WIDGET = 0x003CA37C   # the HUD object it draws on, null between levels
BOUNTY_TARGET = 0x003CA3A8   # what the driver counts the running total toward

# Every award the game makes is this per-level unit times something: a Wanted
# Poster x1, a Golden Sam Statue x2 (the sll at 0x0024C8F4), the destruction
# bonus x0.5, x0.75 and x1 for its three tiers.
#
#   Ice Burg 2,000   Zooney Tunes 1,000   Looney Lagoon 3,000
#   Looningdale's 5,000   Samsonian 7,500   Bank of Samerica 10,000
#   Granny Canyon 25,000   Cartoon Strip-Mine 50,000
#   Taz: Haunted 75,000   Tazland 100,000
#
# So a flat 5,000 is five posters in Zooney Tunes and a twentieth of one in
# Tazland. BOUNTY_STEP is left flat deliberately -- see the note on it.
BOUNTY_UNITS = 0x0046B520    # + lid*4, int32

# a0 == 156 is special-cased at 0x00201FCC: that branch ignores the string and
# prints the level's poster count as "%d / %d" -- collected PLUS ONE, because
# the game only ever raises it at the moment a poster is smashed. Handing it
# to an Archipelago item made the banner read "8 / 7". It only ever lied about
# the number: the sole store in that whole branch (0x00202034) is the banner's
# own state word, so nothing in the save was touched.
#
# Any other index takes the generic path and shows that string as the caption.
# 1393 is "Raise the bounty on your head", already in the game's own table, so
# there is nothing to borrow, nothing to restore, and nothing that can be seen
# in the wrong place later.
#
# -1 is a third option -- 0x00132844 routes it to a strcpy of 0x0048FF10 --
# but that string is the single character "X", so it is worse than a caption
# that means something.
BOUNTY_STRING = 1393
BOUNTY_POSTER_STRING = 156   # what the game's own poster award passes

# The banner without the slow motion. 0x00201F58 writes 0.25 into the popup's
# own factor -- in a DELAY SLOT, so it happens whichever way the branch above
# it goes -- and the driver hands that factor to SetTimeScale at 0x00202830
# once a frame. Writing 1.0 back over it is the smallest intervention there
# is: the banner still animates and the money still counts up, because the
# count-up runs off 0x003CA3AC and not off this.
#
# It has to be HELD, because state 1 decays the factor by 0.005 a frame. At
# the client's poll rate the worst dip between two writes is about 3%.
#
# Only ever for a banner the client raised. The Golden Sam Statue and the
# destruction bonus keep their slow motion; that is the game's presentation
# and not ours to take away -- and notify.slowed() is already there to keep
# our own text from landing on top of it.
BOUNTY_FACTOR = 0x003CA3B0
BOUNTY_QUIET_FOR = 12.0      # longest a banner of ours is worth holding
BOUNTY_QUIET_GRACE = 0.6     # before the driver has set the state word

# THE BANNER IS THREE OBJECTS, not one, and only one of them is the number.
# The popup's constructor at 0x00201518 builds:
#
#   0x003CA37C  a container with a single page, holding tazbiglogo.bmp
#               (0x002016C4 loads the bitmap, 0x00201704 saves the widget at
#               0x003CA38C, 0x00201814 scales it 4.5x)
#   0x003CA388  a container with TWO pages -- "legend page" (0x0049FD70),
#               which holds the caption, and "cash page" (0x0049FD80), which
#               holds the "$" figure
#
# The generic gain path shows both containers (0x00202068 and 0x00202088) and
# starts the second one on the caption; state 2 flips it to the cash page
# about eight tenths of a second later (0x0020232C, a NextPage). For a filler
# item that can arrive at any moment, that is a lot of ceremony for a number.
#
# Both are ordinary widget containers of the same class, vtable 0x00490338,
# and both gate their own per-frame draw on bit 1 of +0x208 -- the bit Show
# sets at 0x0013BD24 and Hide clears at 0x0013BF50, and which 0x0013D444
# tests before recursing into the page's children. Clearing it on the logo's
# container is the whole of "no logo".
BOUNTY_LOGO_BOX = 0x003CA37C
BOUNTY_TEXT_BOX = 0x003CA388
BOX_FLAGS = 0x208
BOX_VISIBLE = 0x2
BOX_SET_PAGE = 0x0013D2B8    # SetPage(this, index) -- vtable +0x158
BOUNTY_CASH_PAGE = 1         # legend page is built first, cash page second
BOUNTY_COUNTING = 3          # the state whose next stop is the count-up
BOUNTY_SHOWING = 4           # the count-up itself

# How long to leave the finished number up. State 4 ends when its expiry
# passes the game clock (0x002024DC c.olt.s / 0x002024E4 bc1t), NOT when the
# total reaches the target -- so pushing the expiry forward simply leaves the
# banner on screen. There is no risk of the counter running past the number
# it was supposed to land on: 0x00202604 only adds while total < target, and
# 0x002026B8 snaps it to the target otherwise.
#
# The game's own 0.3s is fine for an award the player just earned and was
# already looking at. An Archipelago item arrives while they are doing
# something else.
BOUNTY_EXPIRY = 0x003CA3A4
BOUNTY_HOLD = 2.5
GAME_TIME = 0x003FF054

# The level id AS THE POPUP READS IT -- a byte, not the word at LEVEL_ID.
# 0x00201DD0 indexes the save record with this one, so it is the one that has
# to be in range before we call it. The two agree in play; they are read from
# different places and only this one is what the write actually uses.
CURRENT_LEVEL_BYTE = 0x0046DD5C
PREV_LEVEL_BYTE    = 0x0046DD5D

# The banner's state word, 0 when idle. notify.py owns the constant and the
# reasoning behind it; repeated here so bounty_ready does not have to reach
# into a module that may not have imported.
POPUP_STATE_ADDR = 0x003CA3B4


TOTAL_ENEMY_COUNT = 0x46C45C        # decrements when a catcher is defeated

# THE ENEMY LIST IS A LINKED LIST, NOT AN ARRAY. This was wrong for the whole
# project and it is what the Zooney Tunes bee hive catcher was.
#
# The old constants were ENEMY_ARRAY = 0x0046C680, ENEMY_COUNT = 0x0046C720 and
# ENEMY_SLOTS = 40 -- the last one inferred from the gap between the other two.
# All three are the same misreading. 0x0046C680 is not an array: it is the
# `next` field of a circular doubly-linked list sentinel at 0x0046C510.
#
#     0x0046C510  the list head
#        +0x170 -> 0x0046C680    head->next   was read as ENEMY_ARRAY[0]
#        +0x174 -> 0x0046C684    head->prev   was read as ENEMY_ARRAY[1]
#        +0x210 -> 0x0046C720    node count   was read as ENEMY_COUNT
#
# So `ENEMY_ARRAY + i*4` returned the FIRST enemy at i=0, the LAST enemy at
# i=1, and `head+0x178` onwards after that -- other fields of the sentinel,
# permanently zero, dropped by valid_ptr without a word. The count was always
# right. The indexing never was. **catchers() could only ever see two enemies**,
# and anything in the middle of the list was invisible.
#
# That is the bee hive keeper, exactly. keeper05 is the only keeper in Zooney
# Tunes with two other enemies inside the activation radius -- brownbear01 at
# 572 units and brownbear02 at 1355. With three enemies active you can reach
# the newest and the oldest, and keeper05 is the one in the middle. It was
# never flickering. It was never being read. The bears were the cause after
# all, just not by being mistaken for it: they are two extra list nodes.
#
# Verified against ee_dump.bin, not reasoned about: all ELEVEN group heads at
# LEVEL_MANAGER+0x30 close on themselves and every +0x210 equals its walked
# length exactly (0, 11, 378, 67, 0, 129, 0, 0, 21, 0, 4), and the primitives
# were read as instruction words --
#
#   0x0023CA30  Group::Init    head->next = head->prev = head, count = 0
#   0x0023CD40  AddChild       push-FRONT, then count++ at +0x210
#   0x0023CD70  RemoveChild    pure unlink, then count--. No slot is written
#                              and nothing is shifted, so there are no holes,
#                              because there are no indices.
#
# taz_enemylist.py `check` asserts all of it offline against the dump.
LEVEL_MANAGER = 0x46C4E0            # +0x00 is the level name ASCII
GROUP_FIRST   = LEVEL_MANAGER + 0x30
GROUP_STRIDE  = 0x220

# Enemies live in a PAIR of groups and are shuttled between them by their own
# state machine. An enemy is never freed on defeat -- only reparented -- so it
# stays readable for the rest of the level either way.
#
#   0x001633BC  state 0  dormant -> active, if Enemy_ShouldBeActive
#   0x00164178  state 14 active -> dormant, at the end of a despawn
#   0x00162DF8  Enemy_ShouldBeActive: |dy| <= SUB+0x40 AND distance < SUB+0x40,
#               and SUB+0x40 reads 3000.0 on every enemy measured.
ENEMY_ACTIVE  = GROUP_FIRST                     # 0x0046C510, within 3000 units
ENEMY_DORMANT = GROUP_FIRST + GROUP_STRIDE      # 0x0046C730, everything else

L_NEXT  = 0x170
L_PREV  = 0x174
L_OWNER = 0x1E0                     # node -> the group currently holding it
L_COUNT = 0x210

# A walk must terminate even if it reads a torn pointer mid-frame. The largest
# group in a real level holds 378 nodes, so this is far above anything real
# and only exists so a bad read cannot spin.
WALK_CAP = 4096

# Enemy object layout
E_TYPE      = 0x1A0     # ASCII tag: "keep" / "catc"
E_POS       = 0x0C0     # x,y,z floats -- same offset Taz uses
E_SUB       = 0x1D8     # pointer to the state sub-object
E_ANIM      = 0x0B0     # via E_SUB -- the STATE. Single writer, 0x00162B88.
E_DEFEATED  = 0x0CC     # via E_SUB -- note says "Stunned/Defeated", ambiguous
E_NAME      = 0x180     # inline ASCII, "enemy keeper01" .. "enemy keeper06"

# THE PERMANENT DEFEAT BIT, and the reason the judge no longer has to catch
# anything inside a window.
#
# E_DEFEATED (+0x0CC) is NOT a defeat flag. It is a hit/stun latch: four
# setters all store a literal 1 on any hit (0x00161B9C, 0x00161CA0,
# 0x00161D94, 0x00161E68) and ten different state handlers clear it again on
# the way past. It reads 1 during "defeated" only incidentally, which is why
# it has been ambiguous since the note that named it.
#
# E_ALIVE (+0x300) is the real thing. GenericAI::Init sets it to 1
# (0x00160C8C). The state-6 handler has exactly TWO exits and they are
# precisely kill and knockdown:
#
#   0x00163E84  SetEnemyState(enemy, 0xE, 6)  -> despawn, and the very next
#               instruction is `sw $zero, 0x300($s1)` at 0x00163E8C. It stays
#               down. THIS IS THE KILL.
#   0x00163ED0  SetEnemyState(enemy, 3, 6)    -> back to suspicious, and
#               `sw $zero, 0xcc($s1)` clears the latch. It got back up.
#
# The despawn exit is gated on E_READY (+0x304) being set, which is what the
# ~2.9s between "defeated" and "vanishing" in taz_despawn_record.json is: the
# death animation finishing. That recording is also the evidence this path
# runs at all -- it caught E_ANIM 6 -> 14 at t+26.90, and 0x00163E84 is the
# only site in the state-6 handler that passes 14, so the +0x300 store on the
# next instruction executed.
#
# The state-0 handler refuses to reactivate an enemy whose +0x300 is 0
# (0x001633CC), so it is genuinely permanent, and because the object is only
# reparented it is still readable from the dormant group afterwards. There is
# no window to miss.
#
# Two other writers exist and neither is a hazard by accident. The TRIGGER
# script command interpreter at 0x002743A8 can set it (ACTIVATE, 0x0027520C)
# or clear it (DEACTIVATE, 0x00274E44) on an object of class ENEMY, and
# 0x0027EFFC initialises it once at spawn from the scene's `active`
# attribute. No shipped scene string in the dump targets an enemy instance,
# but the judge only ever credits an observed 1 -> 0 TRANSITION, so a keeper
# that was already off when we first looked can never be credited.
E_ALIVE     = 0x300     # via E_SUB: 1 = can still act, 0 = beaten for good
E_READY     = 0x304     # via E_SUB: gates the despawn exit out of state 6

STATE_DEFEATED  = 6     # via E_SUB at E_ANIM
STATE_DESPAWN   = 0xE   # set by the kill exit, one instruction before E_ALIVE

# THE LEASH. A keeper cannot leave a circle around the place it spawned, and
# the game keeps both the centre and the radius on the object:
#
#   E_SUB +0x30   vec3, the leash centre. Written ONCE, in the constructor at
#                 0x00171930, as a straight copy of the spawn position, and
#                 never again except by the despawn path putting the same
#                 value back. The keeper is physically clamped to it: at
#                 0x00163350 the game compares its distance from the centre
#                 against the radius and, if it is outside, snaps its X and Z
#                 back onto the circle.
#   E_SUB +0x48   the radius, 1000.0 on every keeper measured.
#
# This is a better identity than anything the client can observe, and it is
# the answer to a question the judge has had wrong twice. It does not depend
# on when the client started looking, it does not move when the keeper chases
# Taz, and it survives the client attaching mid-level. Checked against all six
# keepers loaded in Zooney Tunes: two match a hand-recorded post exactly, one
# to 0.9 units, one to 18, and two to about 100 -- the recorded positions are
# the ones with the error in them, not these.
#
# +0x40 (3000.0) and +0x44 (1500.0) are the outer and detection radii, kept
# here because they are the neighbours and it is easy to grab the wrong one.
E_HOME      = 0x030     # via E_SUB: the leash centre, vec3
E_LEASH     = 0x048     # via E_SUB: 1000.0, the circle it cannot leave
E_OUTER     = 0x040     # via E_SUB: 3000.0
E_DETECT    = 0x044     # via E_SUB: 1500.0

# HOW CLOSE TAZ HAS TO BE TO WIN. Enemy state 0xA tests his horizontal
# distance against this and takes the keeper to state 6 (defeated) only if it
# is under; otherwise it goes to state 4, pursuing:
#
#   00163D2C  lwc1    $f1, 0xbc($v1)     horizontal distance to Taz
#   00163D30  lwc1    $f0, 0x17c($v1)    250.0
#   00163D34  c.olt.s $f1, $f0
#   00163D3C  bc1f    0x163d54           not closer -> pursue, not defeat
#
# 0x00163D8C is the ONLY one of SetEnemyState's sixteen call sites that passes
# 6, so this gate is upstream of the entire takedown: no defeat, no count
# drop, no costume, no check. It is why a keeper hit from range -- chili
# pepper fire, a burp -- is knocked down rather than beaten, and why the
# client is right to send nothing for one.
E_TAKEDOWN  = 0x17C     # via E_SUB: 250.0
E_DIST_TAZ  = 0x0BC     # via E_SUB: live horizontal distance to Taz
E_DIST_HOME = 0x0B8     # via E_SUB: live distance from the leash centre

KEEPER_ANIM = {2: "idle", 3: "suspicious", 4: "pursuing",
               6: "defeated", 0xE: "despawning"}
ANIM_DEFEATED  = 6
# Not a command on its own. Writing 0xE to E_ANIM and nothing else was tested
# live: the keeper stayed for six seconds with the write still reading back,
# and a second keeper in the same level was already sitting at 14, loaded and
# undefeated. It only means anything as part of DESPAWN_RECIPE below.
ANIM_DESPAWN   = 0xE

# The documented animation list is INCOMPLETE -- 9, 12 and 15 all show up in
# practice, and two different kills were observed ending on 12 and on 14. So
# never key a kill on a specific animation id.
IDLE_ANIMS = {2, 15}
FALL_STATE   = 0x2D      # falling out of the world
CRUSH_STATE  = 0x3E      # crushed
VOID_STATE   = 0x3D      # the void-out itself. Every jump off a ledge in
                         # Granny Canyon read 0x06 (falling) then this, and it
                         # was never in the list -- which is why only the one
                         # death that happened to hit 0x2D ever sent.
HINDENBIRD_LEVEL = 20
BOSS_LOSS_STATE = 0x5A   # losing a boss fight -- right beside CAUGHT
# Taz: Haunted turns Taz into a mouse and then a ball. Both are STATES, not
# costumes -- the costume byte stays 0xFF throughout. Dying while transformed
# does not enter any death state: the ball goes straight to 0x00 and stays
# there, which is why those deaths reported nothing at all.
MOUSE_STATE = 0x51
BALL_STATE  = 0x52
TRANSFORM_STATES = frozenset({MOUSE_STATE, BALL_STATE})

# Nothing may be handed to Taz in either form, nor mid-change.
#
# A powerup writes the costume object, which a transformed Taz does not have;
# a trap writes a state the transform is already driving; and neither shape
# can express most of it anyway. The recording that showed the ball leaving
# +0x1F8 bit 0x1000 clear is the reason to be careful here rather than
# curious -- being a ball is held in fields we do not fully understand.
#
# 0x5D/0x5E/0x5F are the zaps between the forms. An effect landing mid-change
# is the same problem one frame earlier.
ZAP_STATES = frozenset({0x5D, 0x5E, 0x5F})
TRANSFORMED = TRANSFORM_STATES | ZAP_STATES

# The one level this happens in. The rule is deliberately pinned to it: 0x00
# is what the state reads all over the game while anything is loading, so a
# general "transformed -> nothing" rule would report deaths that never
# happened. Narrow and right beats broad and noisy.
HAUNTED_LEVEL = 14

# ------------------------------------------------------------- rollercoaster
#
# The rollercoaster in Cartoon Strip-Mine and Taz: Haunted is the minecart --
# minecart.cpp owns the ride and loads rcoasterlp.wav. Riding is a state of
# Taz's own, entered by a request at +0x10C and then held for the entire ride:
# 19s, 56s and 105s across three recorded rides, never once interrupted.
COASTER_STATE = 0x4D            # STATE_ONMINECART
COASTER_REQUEST = 0x4D          # seen at +0x10C ~0.02s before the state

# How a ride ENDS is what says whether it was a death, and the two endings do
# not share a state. Recorded over two deaths -- a crash and a fall -- and one
# ride survived:
#
#   survived   ONMINECART -> PROJECTILE 0.69s -> GETUPFROMSLIDE 2.48s -> MOVE
#   died       ONMINECART -> MOVE 0.02s -> JUMP 0.35s -> MOVE
#
# So the tell is the cart throwing Taz clear, and it has to be read that way
# round. "Cart straight to MOVE" is the death, but that MOVE lasts 0.02s and
# the client polls every 0.1s, so four times in five it is simply not there to
# see. A death is therefore the ABSENCE of a dismount, which is a thing that
# can be waited for.
COASTER_DISMOUNT = frozenset({
    0x15,       # PROJECTILE       -- flung clear as the ride ends
    0x16,       # PROJECTILESLIDE
    0x04,       # GETUPFROMSLIDE   -- and picking himself back up
})

# Long enough that a dismount cannot be missed -- PROJECTILE alone covers
# seven polls -- and short enough that the death is still obviously the cart's.
COASTER_VERDICT = 0.6

# The same idea for a transform death: long enough that a blip cannot fire it,
# short enough that a genuinely stuck ball is freed before the player has
# walked into the water beside him.
TRANSFORM_VERDICT = 0.5

# Nothing may be handed to Taz until he is off and back on his feet. Measured
# at 3.17s from the cart ending to walking; the remainder is margin.
COASTER_GRACE = 3.5

TNT_STATE    = 0x4F      # eating dynamite. Confirmed by comparing a still
                         # snapshot against one taken mid-explosion.
CAUGHT_STATE = 0x59         # Taz state: captured by a keeper

# Taz: Haunted has two catchers that cage Taz instead of netting him. Recorded:
#
#     0x22 PLAYANIMATION -> 0x54 CAGED -> 0x55 CAGEDMOVE
#
# and no 0x59 anywhere in it, which is why those captures never sent. Both ids
# were already listed in UNSAFE_TO_INTERRUPT as "the rest of the capture
# chain", so something once knew about them; nothing ever reported on them.
#
# Only CAGED fires, and only when it arrives on its own. An ordinary keeper
# nets Taz and cages him afterwards, and that must be one capture rather than
# two -- so a recent net suppresses the cage.
CAGED_STATE = 0x54
CAGED_MOVE_STATE = 0x55
NET_MEMORY = 8.0            # how long a net keeps the cage quiet

# Confirmed by observation: a knocked-down keeper is flagged briefly, then
# gets up and CLEARS the flag. A killed one leaves the enemy array with the
# flag STILL SET. A keeper that merely streams out leaves unflagged. So the
# signature is "gone while flagged", not any particular animation.
LEVEL_ID_ASCII  = 0x46C4E0          # e.g. "icedome", "safari"

# Save files. File 2/3 are the whole save block shifted by this much.

GAME_STATE_NAMES = {
    0: "Booting", 1: "Active", 3: "Paused",
    4: "Ninja Takedown/Results", 5: "Saving/Loading", 6: "Boot Load",
    8: "Load (black)/Cutscene", 9: "Cutscene/Level Results",
    0xE: "Start Level Screen", 0xF: "Credits", 0x10: "Continue Screen",
}
STATE_ACTIVE = 1

# ---------------------------------------------------------------- Taz

O_COSTUME_PTR = 0x1CC
O_STATE_PTR = 0x1C8      # the state object, confirmed live
O_BONUS_PTR   = 0x1C8
O_ANIM_PTR    = 0x134

C_COSTUME     = 0x11C
C_REMOVE      = 0x120        # set when defeating a catcher

# Powerups, all through the costume object (Taz -> +0x1CC). One-byte flags plus
# two float timers. Good filler-item material: each is a single write.
C_BURP_TIME   = 0x140        # float, burp elapsed
C_POWER_TIME  = 0x160        # float, shared by invisibility/hiccup/gum/pepper
C_HICCUPING   = 0x184        # 1 = currently hiccuping
C_INVISIBLE   = 0x194        # 1 = invisibility active
C_HICCUP      = 0x198        # 1 = hiccup active
C_BUBBLEGUM   = 0x19C        # 1 = bubble gum active
C_PEPPER      = 0x1A0        # 1 = pepper active
C_BURP        = 0x1D4        # 1 = burp active

# A powerup pickup writes FOUR things, not just the flag. Captured by diffing
# the costume object across a real pepper:
#
#   +0x160  0 -> 18.98 (float)   duration
#   +0x16C  -1 -> 4              which powerup is active; -1 means none
#   +0x170  garbage -> 2         secondary state
#   +0x1A0  0 -> 1               the flag
#
# Setting only the flag never worked because +0x16C still said "none", so the
# game had nothing to run.
#
# "counting DOWN" was wrong, at least for invisibility. Its tick at 0x001C6790
# ADDS the frame delta to +0x160 (0x001C67C0) and compares upward: past 20.0
# (0x001C67D0) Taz starts blinking, past 25.0 (0x001C68C0) the whole effect
# ends via 0x001C68E8, which clears the flag and puts the material back.
#
# So the game runs invisibility itself, start to finish, and the client used
# to be re-writing 18.98 into that timer every tick. That parked it just under
# the blink threshold, where the tick does nothing -- which is exactly why the
# effect never blinked out and never expired on its own. Holding it was
# solving a problem the game did not have.
#
# +0x164 is the blink phase accumulator and +0x168 the blink half-period,
# 0.75 at grant, shrinking x0.75 per toggle to a 0.25 floor (0x001C6870), so
# the flashing gets faster as it runs out.
C_ACTIVE_ID   = 0x16C        # -1 = none
C_ACTIVE_SUB  = 0x170
C_ACTIVE_NONE = 0xFFFFFFFF
C_BLINK_PHASE = 0x164
C_BLINK_HALF  = 0x168
INVIS_BLINK_AT = 20.0        # 0x001C67B8, lui $at, 0x41a0
INVIS_ENDS_AT  = 25.0        # 0x001C68B4, lui $at, 0x41c8

# How long the player gets. Both thresholds above are constants baked into the
# game's code, so the way to set the length is to choose where the timer
# STARTS: it always ends at INVIS_ENDS_AT, so starting it at 10.0 gives
# fifteen seconds. The blink is always the final five, because that is the gap
# between the two thresholds and not ours to move without patching code.
#
# Change INVIS_SECONDS alone; the rest follows. Anything up to 25.0 is a
# shorter solid phase, anything above starts the timer negative and simply
# runs longer before the same five-second blink.
INVIS_SECONDS = 15.0
INVIS_START = INVIS_ENDS_AT - INVIS_SECONDS        # 10.0
INVIS_BLINK_FOR = INVIS_ENDS_AT - INVIS_BLINK_AT   # 5.0, fixed by the game

# id seen at +0x16C for each powerup. Pepper was recorded; invisibility comes
# out of the game's own grant, which writes 2 at 0x0024C108.
POWERUP_ID = {"pepper": 4, "invisibility": 2}

# +0x170 is NOT part of every grant. The invisibility case of the pickup
# handler (0x0024C0BC..0x0024C108) writes the flag, +0x160, +0x164, +0x168 and
# +0x16C, and nothing else -- there is no store to +0x170 anywhere on it. The
# field currently holds 0x7FFF7F03, which is packed bytes rather than a small
# enum, so writing 2 over it was destroying three bytes of something else for
# no reason. Pepper keeps it, because pepper was recorded doing it.
POWERUP_SUB = {"pepper": 2}

# ------------------------------------------ the material Taz is drawn with
#
# Setting C_INVISIBLE is what makes the game TREAT Taz as invisible. It is not
# what makes him LOOK it, and that is the whole of the bug: the flag was set,
# enemies lost him, and he stayed solid until he spun.
#
# A real pickup does one more thing. At 0x0024C0EC, after the four costume
# fields, it calls 0x0023F3C8(Taz, 0x003AAE80). That function is two calls to
# the setter at 0x0030DFB8, which is four instructions long:
#
#     sll  a1, a1, 2  /  addu v0, a0, a1  /  move a0, v0
#     sw   a2, 0x140(v0)          obj[0x140 + slot*4] = mode
#     sw   a3, 0x150(a0)          obj[0x150 + slot*4] = param
#
# so the entire visual is four words, and they are on the TAZ object -- not
# the costume. That is why looking at the costume found nothing:
#
#     Taz+0x140 = 3       Taz+0x150 = 0
#     Taz+0x144 = 4       Taz+0x154 = 0x003AAE80
#
# 0x003AAE80 is {3.5, 0x80, 0x80, 0x80, 0x80} -- RGBA 128 across, half alpha.
# The renderer reads it every draw: the mode-4 handler at 0x003059A0 loads the
# descriptor at 0x003059AC and pulls R, G, B and A straight out of it into the
# GIF packet. There is no dirty flag and no compare against a previous value,
# so writing the words shows up on the very next frame.
#
# Which is exactly why spinning appeared to fix it. STATE_SPINUP dispatches to
# 0x0024FEF8, which resets the material and then re-reads C_INVISIBLE at
# 0x00250048 and re-applies at 0x00250058. Five paths do that -- spin,
# electrocution, SetModel and two others -- so any of them would have worked.
# Spinning is just the one a player does without meaning to.
#
# Beware the offset collision: 0x140 is C_BURP_TIME on the COSTUME object and
# the material mode array on the TAZ object. Different structs.
O_MAT_MODE  = 0x140          # Taz object: mode[slot],  slots 4 bytes apart
O_MAT_PARAM = 0x150          # Taz object: param[slot], likewise
MAT_INVISIBLE = 0x003AAE80   # half-alpha, the invisibility material

# 0x0023F3C8 skips its second setter when bit 1 of this is set (the branch at
# 0x0023F404). It reads 0 in every dump taken so far and its writer was never
# found, so the bit is checked rather than assumed -- one word, and this then
# does nothing surprising if it is ever set.
MAT_FLAGS = 0x004125D0
MAT_SKIP_SLOT1 = 0x2

# The costume object alone is not enough: a real pickup also puts Taz into a
# per-powerup STATE, and the ability is dispatched from that state. Granting
# only the four costume fields leaves him at 0x0A -- the post-pepper state --
# so pressing square yells instead of breathing fire.
#
# Comparing a real pepper against a granted one showed the difference in two
# places at once, mirrored:
#
#   state +0x200 / bonus +0x0B0   0x3B   the state itself
#   state +0x204 / bonus +0x0B4   0x3B   its request/echo
#   state +0x270 / bonus +0x120   0      a flag the granted version leaves at 1
POWERUP_STATE = {"pepper": 0x3B}
S_STATE_ECHO = 0x204        # in the state object, mirrors +0x200
S_STATE_CLEAR = 0x270       # must be 0 while a powerup state is active
B_STATE = 0x0B0             # bonus object's copy of the state
B_STATE_ECHO = 0x0B4
B_STATE_CLEAR = 0x120
POWERUP_DURATION = 18.98     # seconds, from the observed pepper
S_STATE     = 0x0B0      # the state byte within it
S_REQUEST   = 0x10C      # the state Taz is ASKING for, in the same object
S_HANDLER   = 0x108      # the handler for that state, right beside it

# Eating dynamite. 0x0024B8D0 plays "runeat2", then flips a coin between
# "tntinside" with muffledexplode.wav and "badfood" -- the two animations.
# It has no callers: 0x002C44D8(taz, 0x4F, handler) installs it, writing
# the handler to the state object at +0x108 and asking for state 0x4F.
# Doing both writes ourselves is the whole trap; no injected code.
# The state enum is the game's own, from the pointer table at 0x00473BB0.
# 0x4F is STATE_BADFOOD and 0x1D is STATE_ELECTROCUTED; both have their
# behaviour installed by 0x002C44D8(taz, id, fn), which writes the handler
# to the state object at +0x108 and asks for the id at +0x10C. Doing those
# two writes ourselves is the whole trap -- no injected code.
EAT_BAD_FOOD_FN = 0x0024B8D0
EAT_BAD_FOOD_STATE = 0x4F          # STATE_BADFOOD
ELECTROCUTE_FN = 0x001DF550
ELECTROCUTE_STATE = 0x1D           # STATE_ELECTROCUTED
BUBBLEGUM_FN = 0x0024B7C0
BUBBLEGUM_STATE = 0x3A             # STATE_BUBBLEGUM

# Squashing is the one that is not purely a state. SQUASHTAZ (0x00275430)
# sets STATE_MOVESQUASHED and CLEARS bit 0x40 of [actor + 0x1F8];
# UNSQUASHTAZ (0x002754A8) does nothing but put that bit back, and that is
# the entire recovery. Without it he stays flat forever.
#
# The actor is 0x003FF070, not TAZ_PTR at 0x003FF060 -- the squash and the
# net both drive the second pointer.
ACTOR_PTR = 0x003FF070
O_ACTOR_FLAGS = 0x1F8
SQUASH_BIT = 0x40
SQUASH_STATE = 0x2E                # STATE_MOVESQUASHED
S_ANIM      = 0x0B8      # the animation that goes with the state
IDLE_STATE  = 0x0A       # standing still -- not 0x00, as once recorded
# Every state that means Taz died. Collected here because three separate
# places were listing subsets of it and drifting apart.
DEATH_STATES = frozenset({0x2C, 0x2D, 0x3D, 0x3E, 0x59, 0x5A})
B_COMPLETE    = 0x15C
B_CRATES      = 0x188

COSTUME = {
    0x0: "Ninja", 0x1: "Cowboy", 0x3: "Christmas Reindeer", 0x5: "Surfer",
    0x6: "DJ", 0x7: "Werewolf", 0x9: "Adventurer", 0xA: "Caveman",
    0xB: "Snowboarder", 0xC: "SWAT Officer", 0xD: "Skateboarder",
}
# DEATH_STATES was redefined here as {0x2C, 0x59}, nine lines below the real
# one, so every reader saw the narrow set and four of the six death states
# counted as Taz being ALIVE. That is exactly the drift the comment above the
# real definition was written to stop.

# ---------------------------------------------------------------- levels

REGION_TO_ID = {v: k for k, v in T.LEVEL_IDS.items()}

BOSS_LEVELS = frozenset({7, 12, 17, 19, 20})

BOSSES = {7: "Gossamer", 12: "Daffy", 17: "Sam (Dodge City)",
          19: "Sam (Disco Volcano)", 20: "Tweety"}


# Offsets inside a level block
L_DESTRUCT_BONUS = 0x22C

# +0x224 is the ACCESS field -- the whole gating system, confirmed in game:
#   0x00  locked / never reached
#   0x20  level unlocked (bit 5).  Entering Zooney Tunes set this and opened
#         Ice Burg and Looney Lagoon.
#   0x21  hub or boss unlocked (bits 5 and 0).  Beating Elephant Pong set
#         this on BOTH Elephant Pong and Sam Francisco -- the latter is what
#         makes hub 2 reachable.
#
# Because hub access lives here and not in the boss's own +0x000, the boss
# "defeated" flag stays free to use as a location check.

# A boss opens when all three of its hub's levels have +0x000 set. Clearing
# them re-locks it. Changes take effect on the next hub load.


# Poster names straight from the code notes -- far better location names
# than "Poster 3". Order matches the +0x1E8 array.

# Tazland has no bonus game -- matches the manual apworld's location list.

# ---------------------------------------------------------------- addresses






def field_addr(level_id, offset, save_file=0):
    return T.level_block(level_id, save_file) + offset




def gallery_addr(n, save_file=0):
    """n is 1-10."""
    return GALLERY_BASE + (n - 1) * 4 + save_file * T.FILE_STRIDE


# ---------------------------------------------------------------- reading


class TazPS2:
    """Everything the client needs. `mem` is pcsx2_mem (or anything with the
    same read_u8 / read_u32 / read_bytes / follow API)."""

    def __init__(self, mem):
        self.mem = mem

    # -- context ------------------------------------------------------

    def game_state(self):
        return self.mem.read_u32(GAME_STATE)

    def in_level(self):
        return self.game_state() == STATE_ACTIVE

    def level_id(self):
        return self.mem.read_u32(LEVEL_ID)

    def region(self):
        return T.LEVEL_IDS.get(self.level_id())

    # The game shows the player File 1, 2 and 3; it stores 0, 1 and 2.
    FILE_DISPLAY_BASE = 1

    def file_selected(self):
        """0, 1 or 2 -- or None on the title screen, before one is picked.

        The game keeps this as a SIGNED BYTE and parks -1 in it for "no file
        chosen"; both 0x00201E90 and 0x002B85E8 read it with `lb`.

        This used to be a u32 read clamped to 0..2, which meant -1 came back
        as 255, fell outside the range, and was silently reported as FILE 0.
        So the title screen looked exactly like a loaded file 0 -- and
        anything that wrote to the save region aimed at a real player's file.
        It only ever appeared to work because the three bytes above it read
        zero.
        """
        try:
            v = self.mem.read_u8(CURRENT_FILE)
        except Exception:
            return None
        if v > 127:                     # the signed byte the game writes
            v -= 256
        return v if 0 <= v <= 2 else None

    def file_display(self):
        """What to call the loaded file when talking to the player."""
        f = self.file_selected()
        return None if f is None else f + self.FILE_DISPLAY_BASE

    def save_file(self):
        """The loaded file, clamped, for callers that index the save region.

        Kept clamping so no caller can be handed None and compute a wild
        address from it. Ask file_selected() if you need to know whether a
        file is loaded at all -- the difference is the whole point.
        """
        f = self.file_selected()
        return 0 if f is None else f

    def difficulty(self):
        return self.mem.read_u32(DIFFICULTY)

    # -- save data ----------------------------------------------------

    def level_state(self, level_id, save_file=None):
        """One level's progress. One bulk read, then unpack."""
        if save_file is None:
            save_file = self.save_file()
        base = T.level_block(level_id, save_file)
        buf = self.mem.read_bytes(base, T.SAVE_STRIDE)

        def u32(off):
            return int.from_bytes(buf[off:off + 4], "little")

        return {
            "complete": bool(u32(T.L_COMPLETE)),
            "sandwiches": u32(T.L_SANDWICHES),
            "posters": [bool(u32(T.L_POSTER + i * 4))
                        for i in range(T.POSTERS_PER_LEVEL)],
            "posters_destroyed": u32(T.L_POSTERS_DONE),
            "bounty": u32(T.L_TOTAL_BOUNTY),
            "destruction": u32(T.L_DESTRUCTION),
            "golden_sam": bool(u32(T.L_GOLDEN_SAM)),
            "destruct_bonus": bool(u32(L_DESTRUCT_BONUS)),
            "bonus_game": bool(u32(T.L_BONUS_GAME)),
        }

    # -- catchers -----------------------------------------------------

    def walk_group(self, head):
        """Every node linked into `head`, in list order.

        Order is meaningful but not stable: AddChild is push-front, so index 0
        is the most recently added. Never use it for identity.

        Stops on a bad pointer rather than raising. A torn read mid-frame
        should cost one sample, not the poll -- and unlike the index read this
        replaces, a short walk is visible to the caller as a count mismatch
        rather than being silently indistinguishable from an empty list.
        """
        out = []
        try:
            cur = self.mem.read_u32(head + L_NEXT)
        except Exception:
            return out
        while cur != head and self.mem.valid_ptr(cur) and len(out) < WALK_CAP:
            out.append(cur)
            try:
                cur = self.mem.read_u32(cur + L_NEXT)
            except Exception:
                break
        return out

    def catchers(self):
        """Every keeper in the level -- active OR dormant.

        Each entry: ptr, pos, home, name, anim, defeated, alive, active.

        BOTH groups are walked, which is the other half of the fix. An enemy
        more than 3000 units from Taz is moved to the dormant group, and one
        that has been beaten leaves BOTH -- it is freed, not reparented, which
        a live capture settled after the disassembly suggested otherwise.
        Walking only the active group would mean a keeper vanished from view
        the moment Taz stepped 3000 units away, which is what the whole
        flicker/grace/backstop apparatus was built to survive. Walking both, a
        keeper is visible from the moment the level loads until it is either
        killed or the level unloads, and those two are distinguishable.

        'active' says which group it came from. Nothing needs it to decide a
        takedown; it is there so the trace can say where a keeper was.

        Also sets `walk_ok`. Read it -- see below.
        """
        out = []
        seen = set()
        # INTEGRITY. Each head keeps its own node count at +0x210, maintained
        # by AddChild and RemoveChild, so a walk that does not arrive at
        # exactly that many nodes read a torn pointer: the list was being
        # edited underneath us. The judge has to know, because a short walk is
        # otherwise indistinguishable from enemies having left -- and "the
        # keeper's pointer goes unreadable" was carried as a fact about the
        # game for six sessions when nothing had ever checked whether the read
        # itself was sound.
        self.walk_ok = True
        for head, active in ((ENEMY_ACTIVE, True), (ENEMY_DORMANT, False)):
            nodes = self.walk_group(head)
            try:
                if self.mem.read_u32(head + L_COUNT) != len(nodes):
                    self.walk_ok = False
            except Exception:
                self.walk_ok = False
            for ptr in nodes:
                # A node cannot be in both groups, but a torn read during the
                # reparent could show it twice. Cheaper to dedup than to
                # reason about.
                if ptr in seen:
                    continue
                seen.add(ptr)
                entry = self._read_enemy(ptr, active)
                if entry is not None:
                    out.append(entry)
        return out

    def _read_enemy(self, ptr, active):
        """One enemy, or None if it is not a keeper or cannot be read.

        Every read that can fail is caught, and a field that could not be read
        comes back None rather than a plausible-looking zero. The judge treats
        None as "nothing concluded", which is the only safe reading: a torn
        E_ALIVE reported as 0 would credit a check nobody earned.
        """
        if not self.mem.valid_ptr(ptr):
            return None
        try:
            tag = self.mem.read_bytes(ptr + E_TYPE, 4)
        except Exception:
            return None
        # The notes say the type was probably renamed catcher -> keeper,
        # so accept both spellings.
        if tag not in (b"keep", b"catc"):
            return None
        try:
            sub = self.mem.read_u32(ptr + E_SUB)
        except Exception:
            return None
        if not self.mem.valid_ptr(sub):
            return None
        try:
            pos = self.mem.read_floats(ptr + E_POS, 3)
        except Exception:
            pos = None
        # The leash centre, which is what this keeper IS. Read every tick
        # rather than remembered: it is write-once in the constructor, so
        # it costs three words and can never go stale.
        try:
            home = self.mem.read_floats(sub + E_HOME, 3)
        except Exception:
            home = None
        try:
            raw = self.mem.read_bytes(ptr + E_NAME, 16)
            name = raw.split(b"\0")[0].decode("ascii", "replace")
        except Exception:
            name = ""

        def word(off):
            try:
                return self.mem.read_u32(sub + off)
            except Exception:
                return None

        anim = word(E_ANIM)
        hit = word(E_DEFEATED)
        alive = word(E_ALIVE)
        return {
            "ptr": ptr,
            "pos": pos,
            "home": home,
            "name": name,
            "active": active,
            "anim": anim,
            # The hit/stun latch, kept under its old name because the four
            # conditions still read it. It is not proof of anything on its own.
            "defeated": None if hit is None else bool(hit),
            # The permanent one. None means it could not be read.
            "alive": None if alive is None else bool(alive),
            "addr": sub + E_DEFEATED,
            "alive_addr": sub + E_ALIVE,
        }


# Idle positions repeat exactly, so the first sighting of a keeper is its
# post. Death positions are useless -- keepers chase Taz across the level.
CATCHER_MATCH_RADIUS = 150.0

# How long a level must have been polled before the judge is willing to say
# its recorded positions are wrong. Only that one accusation waits.
SETTLE_SECS = 3.0

# How long after a flagged keeper leaves the array Taz's costume may still come
# off and count as the same takedown. A recording of a real kill:
#
#     t+0.0   the keeper's defeated flag goes 0 -> 1
#     t+3.6   it leaves the array, and TOTAL_ENEMY_COUNT drops by one
#     t+5.1   Taz finally loses the costume
#
# so the gap being confirmed is about a second and a half. Five is generous
# without being long enough for an unrelated costume loss to wander in.
COSTUME_WINDOW = 5.0

# The departure and the count drop were in the same frame in that recording,
# but a poll can land between them either way round, so a little slack.
COUNT_WINDOW = 1.5







# A keeper is only despawned while it is still near its post. One that has
# already left to chase Taz is left alone and dealt with on the next visit,
# when it spawns at home again.
DESPAWN_RADIUS = 1200.0

# HOW to send one away. None means "we do not know yet", and nothing is
# written -- the matching and the bookkeeping still run, so turning this on is
# one line once the recipe is known.
#
# Writing ANIM_DESPAWN (0xE) to E_ANIM was the first guess and it is WRONG.
# Tested live: the keeper stayed put for six seconds with the write still
# reading back, and a second keeper in the same level was sitting at anim 14
# already, loaded and undefeated.
#
# taz_despawn.py then recorded a real takedown, and it says why. Catcher 1 in
# Zooney Tunes, every field of both objects, timestamped:
#
#   t+0.00  the takedown, one frame:  E_ANIM 2 -> 6 ("defeated"), the word
#           beside it (sub+0xB4) 2 -> 6 with it, E_DEFEATED 0 -> 1, the
#           animation data pointer at sub+0xD8 swapped, sub+0xDC restarted
#   t+2.88  the vanish, one frame:  obj+0x1F8 gains bit 0x20, obj+0x1D4 is
#           cleared, obj+0x1F0 goes 0.0 -> -128.0, and only THEN does E_ANIM
#           go 6 -> 14
#   t+2.9..3.6  obj+0x1E4's low half falls 256 -> 118: an alpha fade. The
#           scale at obj+0x014/0xD0/0xD4/0xD8 swells to 1.03 then collapses
#   t+3.69  gone from the array, enemy total 12 -> 11
#
# So anim 14 is the RESULT of the vanish, not its cause. The candidate worth
# trying is obj+0x1F8 bit 0x20 -- and note that 0x1F8 is the same offset as
# Taz's O_ACTOR_FLAGS, where SQUASH_BIT is 0x40, so these share an actor base
# and that word is known to be one the game acts on.
# CONFIRMED live: the keeper faded out and left the array after 1.5s, and
# TOTAL_ENEMY_COUNT went 12 -> 11 with it. This is the vanish frame from the
# recording, copied out whole -- the flag, the rate, and the animation. Anim 14
# on its own does nothing, which is what the first attempt proved; it only
# means anything alongside the flag.
#
# Each entry is (where, offset, op, value). "|" sets bits without disturbing
# the rest of the word, which matters for 0x1F8: it is the shared actor flags
# field, so a plain write would clear whatever else the keeper had set.
DESPAWN_RECIPE = [
    ("obj", 0x1F8, "|", 0x20),          # the flag the vanish begins with
    ("sub", E_ANIM, "=", ANIM_DESPAWN),
    ("sub", E_ANIM + 4, "=", ANIM_DESPAWN),
    ("obj", 0x1F0, "=", 0xC5800000),    # -4096.0, the fade rate, steepened
    ("obj", 0x1E4, "=", 0x01000000),    # and the fade value straight to zero
]

# The last two are the "both" variant of taz_despawn.py, measured on four
# keepers in Zooney Tunes: write to gone in 0.20s every time, which is the
# tool's own poll interval and therefore the floor -- it cannot say whether
# the real figure is 0.2s or 0.02s, only that nothing measurable is left.
#
# The game's own fade runs at -128.0 over 256 steps. Steepening the rate and
# zeroing the value it counts down are two ways at the same thing, and both
# are in because neither costs anything and the second also makes the keeper
# invisible on the frame of the write however the removal races.
#
# What this does NOT fix, and nothing here can: a keeper does not exist in
# memory until Taz is close enough for the game to stream it in. Any pop-in
# still visible is that, not the fade.







def dist2(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


class CatcherJudge:
    """Decides when a catcher has actually been beaten.

    Pure: it is fed observations and hands back kills, and never touches
    memory. That is what lets taz_catcher_test.py exercise the shipped code
    rather than a re-typed copy of it.

    ONE CONDITION. The keeper's permanent defeat bit, E_ALIVE, is observed
    going from set to clear.

    That is the whole thing, and it is worth saying why it can be, because
    what stood here before was four conditions, a backstop, a bank-and-resolve
    pass and a grace period -- roughly four hundred lines, every one of them
    added for a real bug.

    All of it was compensating for a mistake one level down. `catchers()` read
    the enemy list as an array when it is a linked list, so it returned the
    first and last enemy and nothing in between. A keeper it could not see
    could not be watched, so the judge had to infer takedowns from their
    consequences instead: a flag caught before it cleared, a departure, a
    count drop, a costume coming off within five seconds. Each inference had a
    window, each window could be missed, and each thing added to cover a miss
    was another inference with another window.

    With the list walked properly, none of that is necessary:

      * Every keeper is visible from level load to level unload. An enemy is
        never freed on defeat -- only reparented between the active and
        dormant groups -- so there is nothing to catch in flight.

      * E_ALIVE (SUB+0x300) is permanent. GenericAI::Init sets it; the state-6
        handler clears it at 0x00163E8C on the way into a despawn, and the
        state-0 handler refuses to reactivate a keeper whose bit is clear. It
        does not flicker, it does not time out, and it is still readable
        afterwards from the dormant group.

      * It means exactly the right thing. The state-6 handler has precisely
        two exits: SetEnemyState(0xE) -- despawn, bit cleared, it stays down --
        and SetEnemyState(3) -- back to suspicious, hit latch cleared, it got
        up. So the bit distinguishes a kill from a knockdown in the game's own
        terms, which is what four conditions were approximating.

    What this deliberately no longer does, and why each is safe to drop:

      COSTUME.  Condition 4 confirmed a takedown by Taz losing the level
        costume. It was the flaky one -- a five second window against an event
        the player watched happen -- and it is pure corroboration for
        something now observed directly. An enemy hit strips a costume too,
        which is what made it ambiguous in the first place.

      THE COUNT.  Condition 3 wanted TOTAL_ENEMY_COUNT to drop with the
        departure. A count cannot tell a kill from a streamer, which is why it
        was never sufficient alone.

      THE BACKSTOP.  It credited a catcher on the strength of every OTHER
        catcher in the level having satisfied 1+2+3. It needed a pending, so
        it could never help the keeper that was invisible -- the one case it
        was wanted for.

      _bank_unclaimed / _resolve_unclaimed.  A clean costume loss nothing
        explained was banked and cashed once exactly one catcher was left.
        This was written specifically for the bee hive keeper, argued from the
        other end because the judge never saw it. It is the piece this whole
        change exists to delete.

      MISSING_GRACE.  A keeper dropping out of the array for a moment used to
        discard everything known about it. It was not flickering; it was
        being read out of a list head as though it were an array. Nothing
        disappears now.

      THE BLIND-GAP WARNING.  A poll gap could swallow a whole takedown --
        sighting, departure and count drop -- because all three were
        transient. A permanent bit cannot be missed by not looking in time.

    The one thing that IS still refused: a keeper whose bit already reads
    clear the first time it is seen. That is either a keeper beaten before the
    client attached, or the TRIGGER script's DEACTIVATE command, which can
    clear the bit on any object of class ENEMY (0x00274E44). Only an observed
    transition credits anything, so neither can invent a check.
    """

    def __init__(self, posts=None, radius=CATCHER_MATCH_RADIUS,
                 costume_window=COSTUME_WINDOW, count_window=COUNT_WINDOW,
                 level_radius=None):
        self.posts = {int(k): [tuple(p) for p in v]
                      for k, v in (posts or {}).items()}
        # Kept for callers that pass them. Nothing reads them any more:
        # matching from a post needs no radius, despawn_targets asks for
        # DESPAWN_RADIUS by name, and the two windows belonged to conditions
        # that no longer exist.
        self.radius = radius
        self.costume_window = costume_window
        self.count_window = count_window
        # The per-level radius recorded alongside the posts in
        # taz_catchers.json. Used only to annotate the trace -- see
        # _check_nothing_matched.
        self.level_radius = {int(k): float(v)
                             for k, v in (level_radius or {}).items() if v}
        self.credited = {}          # level_id -> set of catcher indices
        self.level = None
        self._reset_level()

    def _reset_level(self):
        # Everything here is per-visit. Re-entering a level rebuilds the enemy
        # objects, so every keeper's bit is set again and every keeper is a
        # first sighting again -- which is correct, and is why a beaten keeper
        # cannot leak across a level change.
        self.homes = {}        # ptr -> its leash centre, the keeper's identity
        self.post_of = {}      # ptr -> catcher index, decided once per keeper
        self.alive = {}        # ptr -> last E_ALIVE we could actually read
        # ptrs seen in state 6, and ptrs whose bit was read clear. Either
        # corroborates a departure; state 6 alone is what says a cleared bit
        # was a defeat rather than a script DEACTIVATE.
        self.seen_down = set()
        self.seen_clear = set()
        # Keepers whose leash centre is outside the level's radius of every
        # recorded post. They are enemies, not catchers, and are ignored
        # entirely -- not tracked, not credited, not warned about one by one.
        self.not_a_catcher = set()
        self._told_unmatched = False
        # When this level was first polled, so _check_nothing_matched can
        # wait for it to settle before accusing the data of being wrong.
        self.level_since = None
        # Post indices already booked for a given keeper, so the two credit
        # paths cannot both book the same kill. Deliberately NOT cleared by
        # _forget: it has to outlive the object it names.
        self.credited_ptrs = set()
        self.despawned = set()
        self.last_now = None
        self.why = []          # trace of the last poll, for the log
        self.lost = []         # (idx, reason) for a takedown seen but unnamed
        self.blind = []        # things the player needs told out loud

    def enter_level(self, level_id):
        if level_id != self.level:
            self.level = level_id
            self._reset_level()

    # -- identity ------------------------------------------------------

    def match_post(self, level_id, pos, radius=None):
        """Which recorded post a position is nearest.

        No radius by default: the closest pair of posts in any level is 2,744
        units apart, so "nearest" is never ambiguous, whereas a radius means a
        keeper seen part-way along its patrol matches nothing and falls through
        to something wrong.
        """
        if not pos:
            return None
        posts = self.posts.get(level_id) or []
        best, best_d = None, None
        for i, p in enumerate(posts):
            d = dist2(pos, p)
            if best_d is None or d < best_d:
                best, best_d = i, d
        if best is None:
            return None
        if radius is not None and best_d > radius ** 2:
            return None
        return best

    # -- what the client has to tell it --------------------------------

    def note_costume_strip(self, now=None):
        """The Costume Strip Trap is about to take the costume.

        Retained because grant_effect calls it, and harmless. The judge no
        longer looks at the costume at all, so the trap can no longer be
        confused for the confirming half of a takedown -- there is no such
        half. Kept as a no-op rather than deleted so the trap's own code does
        not have to change to say something true.
        """

    def note_despawn(self, ptr, now=None):
        """We removed this keeper ourselves.

        Only despawn_targets reads this now. It used to matter to the judge as
        well, because our despawn drops TOTAL_ENEMY_COUNT exactly as a
        takedown does and the drop had to be swallowed. E_ALIVE is not fooled:
        DESPAWN_RECIPE writes the vanish directly and never routes through the
        state-6 handler, so a keeper we sent away still reads alive.
        """
        self.despawned.add(ptr)

    def uncredit(self, level_id, idx):
        """Forget a credit the client refused to send.

        poll banks an index the moment it fires, but the client has the last
        word: catcher_refused stops a check that would be out of logic, most
        often because the level's costume has not arrived yet. Without this the
        judge went on believing that catcher was done, so beating it again
        after the costume DID arrive produced nothing -- for the rest of the
        session, and through leaving and re-entering the level, because
        _reset_level deliberately does not clear `credited`.
        """
        done = self.credited.get(level_id)
        if done:
            done.discard(idx)

    def _nearest(self, level_id, pos):
        posts = self.posts.get(level_id) or []
        if not posts or not pos:
            return float("inf")
        return min(dist2(pos, p) for p in posts) ** 0.5

    def _check_nothing_matched(self, level_id, now):
        """Every keeper in the level was rejected as not-a-catcher.

        The narrow, non-flooding remnant of a warning that used to fire once
        per keeper and say the wrong thing. Seeing keepers and matching NONE
        of them cannot be explained by there simply being other enemies about
        -- it means this level's recorded positions are wrong, and every
        catcher in it will silently never send.

        Once per level, and only once: `_told_unmatched` is not cleared by
        _forget. The version of this that reported per keeper flooded the
        client with a line a minute for the rest of the session, because it
        was true for the rest of the session.
        """
        if self._told_unmatched or self.post_of or not self.not_a_catcher:
            return
        posts = self.posts.get(level_id) or []
        if not posts:
            return
        # Belt to catcher_tick's braces. Even with loads skipped, the id and
        # the enemy lists can disagree for a moment either side of the flip,
        # and this accusation is not urgent enough to risk making it during
        # one. Nothing else in the judge waits, because nothing else is an
        # accusation.
        if self.level_since is None or now - self.level_since < SETTLE_SECS:
            return
        self._told_unmatched = True
        self.blind.append(
            "%d keeper(s) are loaded here and not one of them is within "
            "this level's %s units of any recorded catcher post. The "
            "positions in taz_catchers.json are wrong for this level, so "
            "none of its catchers can send"
            % (len(self.not_a_catcher), self.level_radius.get(level_id)))

    # -- the one entry point -------------------------------------------

    def poll(self, level_id, keepers, costume=None, taz_state=None,
             total=None, complete=True, now=None):
        """One observation. Returns newly credited catcher indices, 0-based.

        `keepers` is what TazPS2.catchers() returns and `complete` is its
        `walk_ok` -- whether every list walked to exactly the length its own
        count field claimed. `costume`, `taz_state` and `total` are accepted
        and ignored; the conditions that read them are gone and the client
        still passes them.

        WHAT A KILL LOOKS LIKE, from a live capture rather than from reading
        the disassembly. Counting both groups every tick while beating one
        keeper in Zooney Tunes and three in Bank of Samerica:

            a1 d10 = 11   a0 d11 = 11   a1 d10 = 11   a2 d9 = 11
            a3  d8 = 11 -> a2  d8 = 10        keeper05 beaten

        Every list-to-list move preserves the total. **Every kill drops it.**
        A defeated enemy is not reparented into the dormant group -- it is
        FREED. This contradicts the static reading that produced the first
        version of this class, and the capture wins.

        It also means E_ALIVE is not the permanent, always-readable flag it
        was taken for: the object it lives on stops existing about three
        quarters of a second after the bit clears. So the bit is an early
        signal, not a durable one, and cannot be the only thing watched.

        TWO PATHS, either of which credits:

          1. E_ALIVE observed going from set to clear. The earliest signal
             there is, and free when it works.

          2. A tracked keeper LEAVING BOTH LISTS, corroborated by having been
             seen in state 6 or with its bit clear. This one cannot be missed
             by polling too slowly, because it is terminal.

        Why state 6 and not state 14 is the corroboration: both a defeat and
        a distance-cull despawn pass through state 14 (0x00163634 sends an
        idle enemy that has gone out of range straight there), so 14 proves
        nothing. State 6 is only ever reached from a hit -- all five guard
        blocks that lead to it require the hit latch set -- and the cull path
        never touches it. "Was hit, went down, and then was freed rather than
        getting back up" is exactly a kill, in the game's own terms.

        THREE THINGS THAT LOOK LIKE A DEPARTURE AND ARE NOT:

          A torn read. A walk that comes up short of its own count is a bad
          sample, not four enemies leaving at once, so `complete` false skips
          departure processing entirely. Nothing else in this project ever had
          an integrity check on the read; not having one is how "the pointer
          goes unreadable" became a theory about the game instead of a bug in
          the client.

          A level unload. Everything vanishes together, so if NOTHING is left
          the whole observation is discarded. A keeper genuinely beaten a
          moment before has already been credited by then -- the free comes
          about 3.7s after the takedown, and walking to the exit takes longer
          than that.

          The distance cull. It reparents rather than freeing, so a culled
          keeper never leaves both lists at all. It is the case the old
          MISSING_GRACE existed for, and it simply does not arise now that
          both groups are walked.
        """
        now = time.time() if now is None else now
        self.enter_level(level_id)
        self.why = []
        self.lost = []
        self.blind = []
        self.last_now = now
        if self.level_since is None:
            self.level_since = now

        done = self.credited.setdefault(level_id, set())
        fired = []
        live = set()

        for k in keepers or ():
            ptr = k["ptr"]
            live.add(ptr)

            # The leash centre, which is what this keeper IS: write-once in
            # the constructor and clamped around, so it does not move when the
            # keeper chases Taz and it survives the client attaching mid-level.
            # Only the current position if that read failed.
            home = k.get("home") or k.get("pos")
            if home is not None:
                self.homes[ptr] = tuple(home)

            if ptr in self.not_a_catcher:
                continue

            if ptr not in self.post_of:
                h = self.homes.get(ptr)
                if h is not None:
                    # WITH THE LEVEL'S RADIUS, which it did not used to have.
                    #
                    # Not every keeper is a catcher. Yosemite Zoo has ONE
                    # recorded post and keepers standing 5000 units from it;
                    # matching without a cutoff filed every one of them under
                    # catcher 1, so beating any of them would have sent the
                    # Tutorial 4 check. It also produced a warning per keeper
                    # saying the recorded position was probably wrong, which
                    # is not what was wrong.
                    #
                    # A cutoff is safe HERE in a way it was not when this
                    # matched on a sighting. The leash centre is write-once in
                    # the constructor and exact: measured against all six
                    # keepers in Zooney Tunes it is 0.0, 0.0, 0.9, 18, 100 and
                    # 102 units from the recorded post, against a level radius
                    # of 1220 and a nearest OTHER post 3051 away. There is no
                    # near miss to lose.
                    idx = self.match_post(level_id, h,
                                          radius=self.level_radius.get(
                                              level_id))
                    if idx is None:
                        self.not_a_catcher.add(ptr)
                        self.why.append("%08X is not a catcher -- %.0f from "
                                        "the nearest post, outside this "
                                        "level's %s"
                                        % (ptr, self._nearest(level_id, h),
                                           self.level_radius.get(level_id)))
                        continue
                    self.post_of[ptr] = idx
                    self.why.append("%08X first seen -> catcher %d"
                                    % (ptr, idx))
                elif ptr not in self.alive:
                    # No leash centre and no position. Track it anyway, and
                    # try again next poll -- if it is beaten before the read
                    # comes good, `lost` is what says so.
                    self.why.append("%08X first seen, position unreadable"
                                    % ptr)

            alive = k.get("alive")

            # CORROBORATION, kept as two separate facts because they answer
            # two different questions. `seen_down` is state 6 and ONLY state 6
            # -- see the docstring for why 14 proves nothing. `seen_clear` is
            # the bit having been read clear at least once.
            if k.get("anim") == STATE_DEFEATED and ptr not in self.seen_down:
                self.seen_down.add(ptr)
                self.why.append("%08X seen in state 6" % ptr)
            if alive is False:
                self.seen_clear.add(ptr)
            elif (alive is True and ptr in self.seen_down
                  and k.get("anim") not in (STATE_DEFEATED, STATE_DESPAWN)):
                # IT GOT BACK UP. The other exit from state 6 puts the keeper
                # in state 3 with the latch cleared and E_ALIVE untouched
                # (0x00163ED0 / 0x00163EDC), and a keeper that got up has not
                # been beaten. Without this the corroboration would be
                # permanent, and a knockdown followed later by a level unload
                # would book a check for a keeper still standing.
                self.seen_down.discard(ptr)
                self.why.append("%08X got back up -- not beaten after all"
                                % ptr)

            if alive is None:
                # Could not be read this tick. Conclude nothing and keep the
                # last value: treating an unreadable bit as clear would credit
                # a check nobody earned, which is the one error worth being
                # paranoid about here.
                continue

            prev = self.alive.get(ptr)
            self.alive[ptr] = alive

            if prev is None:
                # First reading. A keeper ALREADY clear was beaten before we
                # were looking, or was switched off by the level script.
                # Either way there is no transition to credit, and guessing
                # would put a check on the board for something we did not see.
                if not alive:
                    idx = self.post_of.get(ptr)
                    self.why.append("%08X already beaten when first seen -- "
                                    "not crediting catcher %s" % (ptr, idx))
                    self.blind.append(
                        "a keeper was already beaten when the client first "
                        "saw it, so catcher %s was not credited. If you beat "
                        "it this visit, beat it again and it will send"
                        % ("?" if idx is None else idx + 1))
                continue

            if prev and not alive:
                # PATH 1. The bit cleared while we were watching.
                #
                # If we never saw state 6 on the way here, this has the shape
                # of the TRIGGER script's DEACTIVATE (0x00274E44), which
                # clears the bit and touches nothing else. It still credits --
                # a missing check blocks a player, an early one does not, and
                # catcher_refused is downstream -- but it must not be silent.
                if ptr not in self.seen_down:
                    self.blind.append(
                        "catcher %s was credited without the keeper ever "
                        "being seen defeated. The check is probably right, "
                        "but if you did not beat that one, please report it"
                        % self._name(ptr))
                    self.why.append("%08X bit cleared, never seen in state 6"
                                    % ptr)
                self._fire(level_id, ptr, fired, "its defeat bit cleared")

        # PATH 2. Anything tracked that is no longer in either list.
        if complete:
            gone = [p for p in self.alive if p not in live]
            gone += [p for p in self.post_of
                     if p not in live and p not in self.alive]
            gone = list(dict.fromkeys(gone))
            for ptr in gone:
                if ptr in self.seen_down or ptr in self.seen_clear:
                    self._fire(level_id, ptr, fired,
                               "it was seen down and then freed")
                else:
                    # A level unload takes every enemy at once and this is
                    # where they all arrive. None of them is corroborated, so
                    # none of them credits -- which is why there is no
                    # separate unload guard. Killing the LAST enemy in a level
                    # would trip one, and did.
                    self.why.append("%08X left both lists without ever being "
                                    "seen down -- not a takedown" % ptr)
                self._forget([ptr])
        elif keepers is not None:
            self.why.append("a list walk came up short of its own count -- "
                            "bad sample, no departures read from it")

        self._check_nothing_matched(level_id, now)
        # Belt and braces against any future flood: the client reports each
        # of these to the player, so the same sentence twice in one poll is
        # noise by definition.
        self.blind = list(dict.fromkeys(self.blind))

        out = []
        for idx in fired:
            if idx in done:
                # Normal when the player re-beats a keeper they already
                # checked. Not normal at all when a keeper is being matched to
                # the WRONG post -- then a real, unchecked catcher dies into
                # somebody else's already-ticked box, every single time, in
                # total silence. Only the player can tell those apart, so say
                # it and let them.
                self.why.append("catcher %d fired but is already banked" % idx)
                self.blind.append(
                    "a takedown was credited to catcher %d, which is already "
                    "checked. If you have not beaten that one before, this "
                    "keeper is being matched to the wrong post" % (idx + 1))
                continue
            done.add(idx)
            out.append(idx)
        return sorted(out)

    def _name(self, ptr):
        idx = self.post_of.get(ptr)
        return "?" if idx is None else str(idx + 1)

    def _fire(self, level_id, ptr, fired, because):
        """Book a takedown for the keeper at `ptr`, once."""
        if ptr in self.credited_ptrs:
            return
        idx = self.post_of.get(ptr)
        if idx is None:
            # Both the leash read and the position read failed, so the keeper
            # cannot be named. Rare, and reported rather than dropped -- this
            # used to reach nothing but debug logging and so looked identical
            # to nothing happening at all.
            self.why.append("%08X beaten, but matched no post" % ptr)
            self.lost.append((None, "a keeper was beaten but its position "
                                    "could not be read, so the judge could "
                                    "not tell which catcher it was"))
            self.credited_ptrs.add(ptr)
            return
        self.credited_ptrs.add(ptr)
        fired.append(idx)
        self.why.append("%08X beaten -> catcher %d (%s)"
                        % (ptr, idx, because))

    def _forget(self, ptrs):
        """Drop everything remembered about keepers that are gone for good.

        Pointers are reused by the allocator, so leaving a dead one in these
        tables is how a fresh enemy inherits a beaten keeper's state --
        including `credited_ptrs`, which would otherwise refuse to book a
        real kill because some earlier object had lived at that address. It
        only has to survive between the two credit paths seeing the SAME
        keeper, and by the time this runs both have had their look.
        """
        for p in ptrs:
            self.credited_ptrs.discard(p)
            self.alive.pop(p, None)
            self.post_of.pop(p, None)
            self.homes.pop(p, None)
            self.seen_down.discard(p)
            self.seen_clear.discard(p)
            self.not_a_catcher.discard(p)
            self.despawned.discard(p)

    def despawn_targets(self, level_id, already, keepers):
        """Keepers whose check is already banked, as (ptr, index) pairs.

        Matched on the FIRST position seen rather than the current one, and
        only within DESPAWN_RADIUS of the post: a keeper already chasing Taz is
        left alone and caught on the next visit, when it spawns at home again.
        Flagged ones are skipped so this can never race a takedown in progress.
        """
        want = {i for (lid, i) in already if lid == level_id}
        if not want:
            return []
        out = []
        for k in keepers or ():
            ptr = k["ptr"]
            if ptr in self.despawned or k.get("defeated"):
                continue
            home = self.homes.get(ptr) or k.get("pos")
            if home is None:
                continue
            idx = self.match_post(level_id, home)
            if idx is None or idx not in want:
                continue
            # WHICH keeper this is and WHETHER IT IS STANDING THERE are two
            # questions, and this used to answer both with one radius test on
            # one position. That worked only by accident, while `homes` held
            # a first sighting: a keeper seen mid-chase matched no post and
            # fell out. Now that `homes` is the leash centre it always matches
            # a post, so the second question needs asking properly -- against
            # the keeper's CURRENT position. Otherwise one halfway across the
            # level, running at Taz, reads as being at home and vanishes in
            # front of him.
            #
            # This is the same number the game keeps at E_SUB+0xB8.
            pos = k.get("pos") or home
            if dist2(pos, home) > DESPAWN_RADIUS ** 2:
                continue
            out.append((ptr, idx))
        return out

    def count(self, level_id):
        return len(self.credited.get(level_id, ()))


# ---------------------------------------------------------------- self test

# Ground truth straight from the code notes.
_NOTE_BLOCKS = {
    3: 0x400444, 4: 0x40067C, 5: 0x4008B4, 6: 0x400AEC, 7: 0x400D24,
    8: 0x400F5C, 9: 0x401194, 10: 0x4013CC, 11: 0x401604, 12: 0x40183C,
    13: 0x401A74, 14: 0x401CAC, 15: 0x401EE4, 16: 0x40211C, 17: 0x402354,
    18: 0x40258C, 19: 0x4027C4, 20: 0x4029FC,
}
_NOTE_FIELDS = [
    # (level_id, offset, documented address)
    (4, T.L_SANDWICHES, 0x400860), (4, T.L_POSTER, 0x400864),
    (4, T.L_POSTER + 6 * 4, 0x40087C), (4, T.L_POSTERS_DONE, 0x40088C),
    (4, T.L_TOTAL_BOUNTY, 0x400894), (4, T.L_DESTRUCTION, 0x400898),
    (4, T.L_GOLDEN_SAM, 0x4008A4), (4, T.L_BONUS_GAME, 0x4008AC),
    (5, T.L_SANDWICHES, 0x400A98), (5, T.L_POSTER, 0x400A9C),
    (5, T.L_GOLDEN_SAM, 0x400ADC), (5, T.L_BONUS_GAME, 0x400AE4),
    (6, T.L_POSTER + 3 * 4, 0x400CE0), (6, T.L_DESTRUCTION, 0x400D08),
    (9, T.L_POSTER, 0x40137C), (10, T.L_POSTER + 5 * 4, 0x4015C8),
    (11, T.L_POSTERS_DONE, 0x401814), (14, T.L_GOLDEN_SAM, 0x401ED4),
    (15, T.L_POSTER + 2 * 4, 0x4020D4), (16, T.L_DESTRUCTION, 0x402338),
    (18, T.L_POSTER + 6 * 4, 0x40278C), (18, T.L_GOLDEN_SAM, 0x4027B4),
]


def self_test():
    bad = 0
    for lid, want in sorted(_NOTE_BLOCKS.items()):
        got = T.level_block(lid)
        bad += got != want
        if got != want:
            print(f"  MISMATCH block {lid}: 0x{got:06X} vs 0x{want:06X}")
    print(f"  level blocks: {len(_NOTE_BLOCKS) - bad}/{len(_NOTE_BLOCKS)}")

    bad2 = 0
    for lid, off, want in _NOTE_FIELDS:
        got = field_addr(lid, off)
        bad2 += got != want
        if got != want:
            print(f"  MISMATCH {T.LEVEL_IDS[lid]} +0x{off:03X}: "
                  f"0x{got:06X} vs 0x{want:06X}")
    print(f"  field offsets: {len(_NOTE_FIELDS) - bad2}/{len(_NOTE_FIELDS)}")

    for r, names in T.POSTER_NAMES.items():
        assert len(names) == T.POSTERS_PER_LEVEL, r
        assert r in REGION_TO_ID, f"{r} not in T.LEVEL_IDS"
    print(f"  poster names: {len(T.POSTER_NAMES)} levels x 7 = "
          f"{len(T.POSTER_NAMES) * 7} named locations")
    return bad + bad2 == 0

# ==========================================================================
# ON-SCREEN TEXT
# ==========================================================================

#!/usr/bin/env python3
"""
taz_strings.py -- on-screen text for Taz Wanted (PS2).

Display text is UTF-16LE in packed tables, so a replacement can never be longer
than the slot it goes into. Every slot here records its own capacity and the
original string, and writes are refused rather than allowed to overrun into the
next entry.

WHAT IS HERE

  LEVEL_NAMES   the 15 level display names, found by probing which copy of each
                string actually shows on screen
  GATE_TEXT     the message under a boss gate
  MESSAGES      Open / Linear templates, with {n} substituted at write time

Addresses were found by probe and can move between boots, so nothing is trusted
blindly: `verify` checks each slot still holds what we expect and re-locates it
by search if not.

    python taz_strings.py list                 slots and what they hold now
    python taz_strings.py verify               check / relocate
    python taz_strings.py lock 4,6             show LOCKED for those levels
    python taz_strings.py unlock all
    python taz_strings.py gate open
    python taz_strings.py gate linear --n 21
    python taz_strings.py restore              put every original back
"""

import argparse
import json
import os
import sys



# Importable without an emulator so the client can load it during generation
# and in tests. pcsx2_mem calls sys.exit when pine is absent, which raises
# SystemExit rather than Exception, so both are caught.
# Capacity is len(original): UTF-16 in a packed table, so that is the ceiling.
LEVEL_NAMES = {
    5:  (0x6A8C4A, "Zooney Tunes"),
    4:  (0x6A8C64, "Ice Burg"),
    6:  (0x6A8C76, "Looney Lagoon"),
    7:  (0x6A8C92, "Elephant Pong"),
    10: (0x6A8CCA, "Samsonian Museum"),
    9:  (0x6A8CEC, "Looningdale's"),
    11: (0x6A8D08, "Bank of Samerica"),
    12: (0x6A8D2A, "Gladiatoons"),
    16: (0x6A8D5C, "Granny Canyon"),
    15: (0x6A8D78, "Cartoon Strip-Mine"),
    14: (0x6A8D9E, "Taz: Haunted"),
    17: (0x6A8DB8, "Dodge City"),
    18: (0x6A8DCE, "Tazland A-maze-ment Park"),
    19: (0x6A8E00, "Disco Volcano"),
    20: (0x6A8E1C, "The Hindenbird"),
}

# The whole table lives here; a search for a moved name only needs this range.
NAME_TABLE = (0x6A8C00, 0x6A8F00)

# Free-text slots. name -> (address, capacity in characters)
#
# Capacity is the length of the original string: UTF-16 in a packed table, so a
# longer replacement eats the next entry.
# Mode-aware gate slots. name -> (address, capacity)
#
# 0x6AAADE holds the same string as 0x6BAF40 -- duplicate copies of the one
# hub-boss line -- so only the live copy at 0x6BAF40 is written.
GATE_TEXT = {
    "hub_boss":  (0x6BAF40, 71),     # Elephant Pong / Gladiatoons / Dodge City
    "she_devil": (0x6BB46C, 58),
    "daffy":     (0x6BB206, 51),
}

# Main menu items. Both read "Start Game" originally.
#
# CAPACITY IS UNVERIFIED for these two: they sit below 0x6A8000, outside the
# exported region, so the space after each string has not been measured.
# "Archipelago" is 11 characters against a 10-character original, so it only
# works if the slot has a spare pair of bytes. Check with:
#     taz_text.py wdump 0x6A3A74 --len 40
# and set the real capacity here. Fitting alternatives at 10 or fewer:
# "AP RANDO", "Archi Rando", "AP MODE".
# Bumped each release; the version line is rebuilt from it.
AP_VERSION = "1.0.0"
VERSION_LINE = f"Taz Wanted Archipelago Version {AP_VERSION}"

# Each boss door shows a line of advice once its level is unlocked. In Open
# mode that line has to say the unlock is missing instead, and go back to the
# original once the boss is granted -- the player will otherwise be told how to
# start a fight they cannot reach.
#
# Addresses to be filled in: search for the vanilla string with
#     taz_strings.py find "Jump in the cement mixer"
# and put the result here. Capacity is the vanilla line's length, since the
# table is packed.
BOSS_HINT = {
    7:  {"addr": 0x6BB198,
         "vanilla": "Finally here's your chance! Jump into that snowblower!",
         "locked": "You need the Elephant Pong level unlock!"},
    12: {"addr": 0x6BB400,
         "vanilla": "Jump in the cement mixer and it will make you a star!",
         "locked": "You need the Gladiatoons level unlock!"},
    17: {"addr": 0x6BB632,
         "vanilla": "Head for the Ammos Dump using the minecart!",
         "locked": "You need the Dodge City level unlock!"},
}

# The hub each boss door stands in. The line is only rewritten while the
# player is there, and put back on the way out, so nothing else that shares the
# text region is disturbed.
BOSS_HINT_HUB = {7: 3, 12: 8, 17: 13}

# ------------------------------------------------------ the boss door panel
#
# A boss door does not have ONE line. It has five, and the game chooses between
# them every time the player walks into the trigger. Writing the first one --
# which is what this used to do, for all three doors -- puts the text somewhere
# the player will almost never see it. That is why Linear never showed a poster
# count.
#
# The selector is 0x00266F00 (endoflevelstats.cpp), reached through the trigger
# dispatcher 0x00273BF8 -> 0x00273F58 on a trigger named "stats;<bossname>",
# guarded to the hubs. In words, and read off the branches rather than guessed:
#
#     all three of the hub's levels complete            -> line 5
#     else the level the player was in IMMEDIATELY
#       before this hub is one of those three AND is
#       marked complete                                 -> that level's line
#     else                                              -> line 1
#
# "the level immediately before" is a byte at 0x0046DD5D, written at
# 0x002BAC28 as the previous value of the current-level byte -- which is
# exactly Caleb's "the text changes depending on which level they just beat".
#
# It also refuses to build the panel at all if bit 0 of block(bossLid)+0x224 is
# set. What that bit means is not established.
#
# The whole panel -- object, choice and text -- is rebuilt from scratch on
# every trigger, so replacing the text is enough and the selector does not need
# patching. We replace ALL FIVE, so whichever one it picks says what we want.
#
# Strings are reached through a table of 1621 entries at 0x0069D250, stride
# 0x10: text pointer, length, wav name, zero. Every entry is packed against the
# next with a single NUL and no slack whatsoever -- verified across all 1620
# consecutive pairs -- so writing in place is capped at the current length, and
# Dodge City's shortest line is 29 characters. Which is not enough for a
# sentence.
#
# So the POINTER is moved instead of the text. Nothing in the game reads the
# length field; every consumer treats the text as a NUL-terminated wide string.
# Restoring is putting a known pointer back, which cannot half-succeed the way
# rewriting a packed table can.
STR_TABLE = 0x0069D250
STR_STRIDE = 0x10
DOOR_TEXT_BUF = 0x01F01400      # notify's scratch page, above its text buffer
DOOR_TEXT_SLOT = 0x200          # per door, so two never share a buffer
DOOR_TEXT_CAP = 200

# boss id -> the hub it stands in, and its five lines as
# (table index, the shipping text pointer, the shipping length).
# Indices and pointers read out of the table itself, not from a name.
BOSS_DOOR = {
    7: {"hub": 3, "lines": [
        (1263, 0x6BAF40, 70), (1264, 0x6BAFCE, 60), (1265, 0x6BB048, 80),
        (1266, 0x6BB0EA, 86), (1267, 0x6BB198, 54)]},
    12: {"hub": 8, "lines": [
        (1268, 0x6BB206, 51), (1269, 0x6BB26E, 84), (1270, 0x6BB318, 42),
        (1271, 0x6BB36E, 72), (1272, 0x6BB400, 53)]},
    17: {"hub": 13, "lines": [
        (1273, 0x6BB46C, 58), (1274, 0x6BB4E2, 29), (1275, 0x6BB51E, 65),
        (1276, 0x6BB5A2, 71), (1277, 0x6BB632, 42)]},
}

# The scratch page notify.py owns. A pointer anywhere in it is one of ours and
# is safe to overwrite; a pointer that is neither ours nor the shipping one
# belongs to something else and is left alone.
DOOR_SCRATCH_LO, DOOR_SCRATCH_HI = 0x01F00000, 0x01F02000

MENU_TEXT = {
    # 0x6A3A74 ("Start Game") is deliberately left alone.
    "start_ap": (0x6A6486, "Start Game", "Start AP", 10),
    # The menu blurb becomes the version banner. Capacity 45, so the version
    # string has room to grow to two-digit parts.
    "version":  (0x6A6582, "Start a new game, or continue an existing one.",
                 VERSION_LINE, 45),
}

# One-off replacements that just get applied and left. The three message-box
# lines are the player-facing "you sent an AP item" text.
STATIC_EDITS = {
    0x6AA7F8: ("Well Done! You've found the secret item! But Sam's just "
               "raised the bounty on you!",
               "Well Done! You've found the secret item! You've sent an "
               "AP Item!"),
    0x6AA89C: ("Congratulations! You've got 100 sandwiches! Now you can play "
               "the bonus game!",
               "Congratulations on finding all sandwiches! You've sent an "
               "AP Item!"),
    0x6AA956: ("Fantastic! You've reached your destruction bonus! ",
               "Fantastic destruction! You've sent an AP Item! "),
}

# {n} and {s} are filled in at write time; {s} is the plural suffix, so one
# remaining poster reads "1 more wanted poster" rather than "posters".
# Per-slot wording, because each slot has a different capacity and the phrasing
# has to fit the smallest case. {n} is the count, {s} the plural suffix.
#
#   hub_boss   71  roomy
#   she_devil  58  comfortable
#   daffy      51  tight -- the obvious "You can't face Daffy until you have
#                  the level unlock." is 53, two over, so it is shortened
GATE_MESSAGES = {
    "hub_boss": {
        "open":   "You can't face the boss until you've found the level unlock.",
        "linear": "You can't face the boss until you've collected {n} more "
                  "wanted poster{s}.",
    },
    "she_devil": {
        "open":   "To rescue She-Devil you'll need to find the level unlock.",
        "linear": "To rescue She-Devil you'll need {n} more wanted poster{s}.",
    },
    "daffy": {
        "open":   "You can't face Daffy without the level unlock.",
        "linear": "You're missing {n} wanted poster{s} to face Daffy.",
    },
}

# Kept for compatibility with earlier calls.
MESSAGES = GATE_MESSAGES["hub_boss"]

LOCKED_TEXT = "LOCKED"

# The Hindenbird's name slot is only 14 characters, but a goal summary is much
# longer than that. Writing past the end works -- the text renders correctly --
# because what follows is menu and results text that is never on screen in
# Tazland: "Complete.", "Continue.", "Exit.", "Round", "resume...", "Quit."
# and so on.
#
# So the overflow is safe ONLY while the player is in Tazland, and the bytes
# have to be put back before they can reach anything that uses them. Nothing
# here writes without first taking a copy.
HB_NAME_ADDR = 0x6A8E1C
HB_OVERFLOW = 0x120          # how far past the slot a long line may reach
_hb_backup = None            # raw bytes, or None if nothing is overwritten


def hb_backup_taken():
    return _hb_backup is not None


def set_hindenbird_text(text):
    """Write a long line over the Hindenbird name and its neighbours.

    Takes a copy first, so `restore_hindenbird` can put every following string
    back exactly. Call it only while the player is in Tazland.
    """
    global _hb_backup
    raw = text.encode("utf-16-le") + b"\0\0"
    span = max(len(raw), HB_OVERFLOW)
    if _hb_backup is None:
        try:
            _hb_backup = mem.read_bytes(HB_NAME_ADDR, span)
        except Exception as e:
            return False, f"could not back up: {e}"
    try:
        mem.write_bytes(HB_NAME_ADDR, raw)
    except Exception as e:
        return False, str(e)
    return True, f"wrote {len(text)} chars ({len(raw)} bytes)"


def restore_hindenbird():
    """Put back everything the long line overwrote."""
    global _hb_backup
    if _hb_backup is None:
        return False, "nothing to restore"
    try:
        mem.write_bytes(HB_NAME_ADDR, _hb_backup)
    except Exception as e:
        return False, str(e)
    _hb_backup = None
    return True, "restored"


def hindenbird_goal_text(posters=0, bosses=0, unlock=False):
    """"Missing 100 Posters, 4 Bosses, & Level Unlock", with only the parts
    the player's goal actually uses."""
    parts = []
    if posters > 0:
        parts.append(f"{posters} Poster" + ("" if posters == 1 else "s"))
    if bosses > 0:
        parts.append(f"{bosses} Boss" + ("" if bosses == 1 else "es"))
    if unlock:
        parts.append("Level Unlock")
    if not parts:
        return "The Hindenbird"
    if len(parts) == 1:
        body = parts[0]
    elif len(parts) == 2:
        body = f"{parts[0]} & {parts[1]}"
    else:
        body = ", ".join(parts[:-1]) + f", & {parts[-1]}"
    return f"Missing {body}"


def connect():
    if not mem.hook():
        sys.exit("Could not reach PCSX2. Booted, with PINE enabled?")
    try:
        print(f"connected  ({mem.game_id()})")
    except Exception:
        print("connected")


# ---------------------------------------------------------------- utf-16


def read_w(addr, max_chars=96):
    try:
        raw = mem.read_bytes(addr, max_chars * 2)
    except Exception:
        return ""
    out = []
    for i in range(0, len(raw) - 1, 2):
        cp = raw[i] | (raw[i + 1] << 8)
        if cp == 0:
            break
        out.append(chr(cp))
    return "".join(out)


def write_w(addr, text, capacity):
    """Write UTF-16LE, clearing exactly the slot and no further.

    Overrunning destroys the next table entry, so a too-long string is an error
    rather than something to truncate silently -- truncated UI text is a bug
    the player sees, and a corrupted neighbour is a bug that crashes.
    """
    if len(text) > capacity:
        raise ValueError(f"{text!r} is {len(text)} chars, capacity {capacity}")
    raw = text.encode("utf-16-le")
    span = (capacity + 1) * 2
    mem.write_bytes(addr, raw + b"\0" * (span - len(raw)))
    return read_w(addr)


def find_w(text, lo, hi):
    pat = text.encode("utf-16-le")
    hits, addr, CH = [], lo, 0x20000
    while addr < hi:
        n = min(CH, hi - addr)
        try:
            buf = mem.read_bytes(addr, n)
        except Exception:
            addr += n
            continue
        pos = buf.find(pat)
        while pos != -1:
            if (addr + pos) % 2 == 0:
                hits.append(addr + pos)
            pos = buf.find(pat, pos + 1)
        addr += n
    return hits


# ---------------------------------------------------------------- state

# Where the original strings are kept so they can be put back. It sat in
# taz_strings.py and did not survive the merge, which left load_state raising
# NameError the first time text was restored.
STATE_FILE = "taz_strings_state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"names": {}, "gates": {}}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- slots


def name_addr(lid, relocate=True):
    """Address of a level's display name, re-locating it if it has moved."""
    addr, orig = LEVEL_NAMES[lid]
    live = read_w(addr)
    if live in (orig, LOCKED_TEXT) or live.startswith(LOCKED_TEXT):
        return addr
    if not relocate:
        return addr
    hits = find_w(orig, *NAME_TABLE)
    return hits[0] if hits else addr


def set_level_name(lid, text):
    addr, orig = LEVEL_NAMES[lid]
    addr = name_addr(lid)
    return addr, write_w(addr, text, len(orig))


def gate_message(mode, n=None, slot="hub_boss"):
    """Render a gate message. {n} is the count, {s} the plural suffix."""
    msgs = GATE_MESSAGES.get(slot)
    if msgs is None:
        raise ValueError(f"unknown slot {slot!r}; "
                         f"known: {', '.join(GATE_MESSAGES)}")
    if mode not in msgs:
        raise ValueError(f"mode must be one of {', '.join(msgs)}")
    tpl = msgs[mode]
    if "{n}" not in tpl:
        return tpl
    if n is None:
        raise ValueError("linear mode needs a poster count (--n)")
    return tpl.format(n=n, s="" if n == 1 else "s")


def apply_all_gates(mode, n=None):
    """Set every mode-aware gate slot at once."""
    out = []
    for slot in GATE_MESSAGES:
        try:
            text = gate_message(mode, n, slot)
            addr, cap = GATE_TEXT[slot]
            if len(text) > cap:
                out.append((slot, False,
                            f"{len(text)} chars vs capacity {cap}"))
                continue
            before = read_w(addr)
            write_w(addr, text, cap)
            out.append((slot, True,
                        f"0x{addr:06X} ({len(text)}/{cap}) {text}"))
        except ValueError as e:
            out.append((slot, False, str(e)))
    return out


def apply_menu_text():
    """Rename the menu entries. Returns [(slot, ok, detail)].

    These do not redraw while the menu is on screen -- the game builds the text
    once -- so the client should write them continuously from startup and the
    player should have it running before they reach the menu.
    """
    out = []
    for slot, (addr, orig, new, cap) in MENU_TEXT.items():
        if slot == "version":
            new = VERSION_LINE          # always current, even if edited above
        live = read_w(addr)
        if len(new) > cap:
            out.append((slot, False,
                        f"{new!r} is {len(new)} chars, capacity {cap}"))
            continue
        try:
            write_w(addr, new, cap)
            out.append((slot, True, f"0x{addr:06X} {live!r} -> {read_w(addr)!r}"))
        except ValueError as e:
            out.append((slot, False, str(e)))
    return out


def apply_static_edits():
    """Apply the fixed message-box replacements."""
    out = []
    for addr, (orig, new) in STATIC_EDITS.items():
        live = read_w(addr)
        cap = len(orig)
        if len(new) > cap:
            out.append((addr, False,
                        f"{len(new)} chars vs capacity {cap}"))
            continue
        try:
            write_w(addr, new, cap)
            out.append((addr, True, f"{live[:32]!r}... -> {new[:32]!r}..."))
        except ValueError as e:
            out.append((addr, False, str(e)))
    return out


def set_gate_text(slot, text):
    if slot not in GATE_TEXT:
        raise ValueError(f"unknown gate slot {slot!r}; "
                         f"known: {', '.join(GATE_TEXT)}")
    addr, cap = GATE_TEXT[slot]
    return addr, write_w(addr, text, cap)


# ---------------------------------------------------------------- commands


def cmd_list():
    print(f"\n  level names   table 0x{NAME_TABLE[0]:06X}-0x{NAME_TABLE[1]:06X}\n")
    print(f"  {'id':>3}  {'address':<10} {'cap':>3}  {'live':<26} original")
    for lid in sorted(LEVEL_NAMES, key=lambda k: LEVEL_NAMES[k][0]):
        addr, orig = LEVEL_NAMES[lid]
        live = read_w(addr)
        flag = "" if live in (orig, LOCKED_TEXT) else "   <- unexpected"
        print(f"  {lid:>3}  0x{addr:06X}   {len(orig):>3}  "
              f"{live!r:<26} {orig!r}{flag}")

    print("\n  free text slots\n")
    for k, (addr, cap) in GATE_TEXT.items():
        print(f"    {k:<16} 0x{addr:06X}  capacity {cap}")
        print(f"      now: {read_w(addr)!r}")

    print("\n  messages\n")
    cap = GATE_TEXT["hub_boss"][1]
    for k in MESSAGES:
        samples = [gate_message(k, n) for n in ((1, 21) if "{n}" in MESSAGES[k]
                                               else (None,))]
        worst = max(len(x) for x in samples)
        fit = "fits" if worst <= cap else f"TOO LONG for {cap}"
        print(f"    {k:<8} (up to {worst} chars, {fit})")
        for x in samples:
            print(f"      {x}")
    print()


def cmd_verify():
    print()
    moved = ok = lost = 0
    st = load_state()
    for lid in sorted(LEVEL_NAMES):
        addr, orig = LEVEL_NAMES[lid]
        live = read_w(addr)
        if live in (orig, LOCKED_TEXT):
            ok += 1
            continue
        hits = find_w(orig, *NAME_TABLE)
        if hits:
            print(f"    {orig!r} moved 0x{addr:06X} -> 0x{hits[0]:06X}")
            st.setdefault("names", {})[str(lid)] = f"0x{hits[0]:06X}"
            moved += 1
        else:
            print(f"    {orig!r} not found (reads {live!r}) -- "
                  f"is the right area loaded?")
            lost += 1
    save_state(st)
    print(f"\n  {ok} in place, {moved} relocated, {lost} not found\n")


def cmd_lock(ids, locked):
    st = load_state()
    print()
    for lid in ids:
        if lid not in LEVEL_NAMES:
            print(f"    no name slot for level {lid}")
            continue
        addr, orig = LEVEL_NAMES[lid]
        text = LOCKED_TEXT if locked else orig
        try:
            a, got = set_level_name(lid, text)
            print(f"    {orig!r:<28} 0x{a:06X} -> {got!r}")
            st.setdefault("names", {})[str(lid)] = f"0x{a:06X}"
        except ValueError as e:
            print(f"    {orig!r}: {e}")
    save_state(st)
    print()


def cmd_gate(mode, n, slot):
    try:
        text = gate_message(mode, n, slot)
    except ValueError as e:
        sys.exit(f"\n  {e}\n")
    addr, cap = GATE_TEXT[slot]
    if len(text) > cap:
        sys.exit(f"\n  {text!r} is {len(text)} chars, slot holds {cap}\n")
    before = read_w(addr)
    _, got = set_gate_text(slot, text)
    print(f"\n  {slot}  0x{addr:06X}")
    print(f"    was: {before!r}")
    print(f"    now: {got!r}  ({len(text)}/{cap} chars)\n")


def cmd_restore():
    print()
    for lid in sorted(LEVEL_NAMES):
        addr, orig = LEVEL_NAMES[lid]
        a = name_addr(lid)
        try:
            write_w(a, orig, len(orig))
            print(f"    0x{a:06X}  -> {orig!r}")
        except ValueError as e:
            print(f"    0x{a:06X}  {e}")
    print()


def parse_ids(args):
    out = []
    for chunk in args:
        for tok in chunk.split(","):
            tok = tok.strip()
            if tok.lower() == "all":
                return sorted(LEVEL_NAMES)
            if tok.isdigit():
                out.append(int(tok))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("list", "verify", "lock", "unlock",
                                    "gate", "gates", "restore",
                                    "menu", "messages", "all"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--n", type=int, default=None,
                    help="poster count for linear mode")
    ap.add_argument("--slot", default="hub_boss")
    a = ap.parse_args()

    connect()
    if a.cmd == "list":
        cmd_list()
    elif a.cmd == "verify":
        cmd_verify()
    elif a.cmd in ("lock", "unlock"):
        ids = parse_ids(a.args)
        if not ids:
            sys.exit(f"usage: taz_strings.py {a.cmd} 4,6  |  {a.cmd} all")
        cmd_lock(ids, a.cmd == "lock")
    elif a.cmd == "gate":
        if not a.args:
            sys.exit("usage: taz_strings.py gate open | gate linear --n 21")
        cmd_gate(a.args[0], a.n, a.slot)
    elif a.cmd == "gates":
        if not a.args:
            sys.exit("usage: taz_strings.py gates open | "
                     "gates linear --n 21")
        print()
        for slot, ok, detail in apply_all_gates(a.args[0], a.n):
            print(f"    {'ok  ' if ok else 'FAIL'} {slot:<12} {detail}")
        print()
    elif a.cmd == "menu":
        print()
        for slot, ok, detail in apply_menu_text():
            print(f"    {'ok  ' if ok else 'FAIL'} {slot:<20} {detail}")
        print()
    elif a.cmd == "messages":
        print()
        for addr, ok, detail in apply_static_edits():
            print(f"    {'ok  ' if ok else 'FAIL'} 0x{addr:06X}  {detail}")
        print()
    elif a.cmd == "all":
        print("\n  menu")
        for slot, ok, detail in apply_menu_text():
            print(f"    {'ok  ' if ok else 'FAIL'} {slot:<20} {detail}")
        print("\n  messages")
        for addr, ok, detail in apply_static_edits():
            print(f"    {'ok  ' if ok else 'FAIL'} 0x{addr:06X}  {detail}")
        print()
    elif a.cmd == "restore":
        cmd_restore()
    mem.un_hook()

# ==========================================================================
# READING, WRITING AND ENFORCEMENT
# ==========================================================================

#!/usr/bin/env python3
"""
taz_game.py -- everything that touches the game's memory.

The client above this knows about Archipelago and nothing about addresses; this
knows about addresses and nothing about Archipelago. The split matters because
almost every awkward bug in this project has lived in the seam between "what
the game is doing" and "what the server thinks", and keeping them apart means
each can be tested on its own.

WHAT IT OWNS

  reading     which save file, which level, difficulty, and which locations
              are currently satisfied
  writing     level and hub access, boss gating, the phantom costume that
              locks a booth, the sandwich spoof that fakes a bonus unlock,
              and the on-screen text
  watching    deaths, for DeathLink

WHAT IT DOES NOT OWN

  What has already been sent, what has been received, or what any of it means.
  The client decides that and hands down a set of granted items; this applies
  them. That is what makes a reload safe: nothing here infers progress from
  the save file, so a save state or a fresh file cannot cause a misfire.
"""

import math
import struct
import time


# `optional` because pcsx2_mem calls sys.exit when pine is absent, and
# generation on a server has no emulator. Everything that reads or writes
# checks for None first.

# --- access -----------------------------------------------------------------

HUB_GROUPS = {3: ([4, 5, 6], 7), 8: ([9, 10, 11], 12), 13: ([14, 15, 16], 17)}

# Levels whose access field must stay open regardless: Zooney Tunes because
# hub 1 derives its other two doors from it, and Tazland because the final
# boss doors sit at its entrance. Both are gated by geofence instead.
ALWAYS_OPEN = {5, 18}
OPEN_VALUE = {5: 0x21}

BOSS_ACCESS = {19, 20}

# The unused costume id. Holding it there makes a phone booth behave as though
# Taz is already dressed, so it refuses him -- a native lock with nothing to
# strip afterwards.
PHANTOM_COSTUME = 0x2

LEVEL_COSTUME = {
    10: 0x0, 16: 0x1, 3: 0x3, 6: 0x5, 9: 0x6,
    14: 0x7, 15: 0x9, 18: 0xA, 4: 0xB, 11: 0xC, 5: 0xD,
}
COSTUME_NAMES = {
    0x0: "Ninja", 0x1: "Cowboy", 0x3: "Christmas Reindeer", 0x5: "Surfer",
    0x6: "DJ", 0x7: "Werewolf", 0x9: "Adventurer", 0xA: "Caveman",
    0xB: "Snowboarder", 0xC: "SWAT Officer", 0xD: "Skater",
}
COSTUME_BY_NAME = {v: k for k, v in COSTUME_NAMES.items()}

# The tutorial popups only appear on Standard; clearing the bit is harmless
# otherwise.
TUTORIAL_FLAG = 0x40404C
TUTORIAL_BIT = 0x01

FIRST_INWORLD_LEVEL_ID = 3
LOAD_STATES = {5}

DIFFICULTY_NAMES = {0: "standard", 1: "advanced", 2: "expert"}

CAUGHT_STATE = 0x59
DROWN_STATE = 0x2C
RESPAWN_JUMP = 1500.0

# ---------------------------------------------------------- bonus game gate
#
# The game decides whether a hub builds a bonus game portal in exactly one
# place, and it is not the sandwich count -- the count is only what that place
# happens to READ.
#
#   0x0028A4B0   mapfile.cpp, the SPECIALTYPE_POLICEBOX case of the map object
#                constructor -- the only caller. It passes the target level's
#                name, and builds nothing at all if the answer is zero.
#   0x0021C8B8   the gate itself. It turns the name into a level id, lets
#                anything that is not one of the nine bonus levels straight
#                through, and otherwise jumps through a table at 0x004A16E0
#                into a nine-way switch that loads that level's sandwich count
#                and finishes at `slti v1,v1,0x64` -- 100 or more, open.
#
# We replace the seven words of that switch dispatch with a read of our own
# nine-byte table, so the answer comes from the server's granted list and the
# count is never consulted. Which means the count never has to be a lie.
#
#     0021C8F0  beq   v0, zero, 0x0021CA24   (kept: not a bonus level -> 1)
#     0021C8F4  lui   v0, 0x01F0             <- from here, seven words
#     0021C8F8  addu  v0, v0, v1                v1 is still (id - 21), 0..8
#     0021C8FC  lbu   v0, 0x0A00(v0)            our byte
#     0021C900  beq   v0, zero, 0x0021C9F4      the game's own "return 0"
#     0021C904  nop
#     0021C908  b     0x0021CA24                the game's own "return 1"
#     0021C90C  nop
#
# Both exits are the shipping ones, so the stack frame is unwound by the code
# that set it up. Nothing branches into those seven words -- checked against
# every branch and jump in the dump -- and no jump table entry points inside
# them, so the switch bodies below are simply never reached.
#
# The failure mode is the same one notify.py documents: if PCSX2 is still
# running an older translation of the block it keeps the shipping behaviour.
# That is why the sandwich spoof stays underneath. Missing portal at worst,
# never a crash.
# ------------------------------------------------- the Standard-only prompts
#
# Zooney Tunes' popups are the PROMPT BOOK (prompt.cpp) -- objective1..5,
# firstZookeeper, firstPoster, lastPoster, sandwichHint, destructionHint,
# statueHint. Not the Bugs voice lines at level start; those are the intro
# camera flythrough and play on every difficulty (see INTROCAM_COUNT).
#
# 0x002B8E58 builds the book. It jumps through a table at 0x004B1280 indexed
# by level id, and only two levels in the whole game land on the case that
# consults the difficulty:
#
#     lid  5 safari   (Zooney Tunes)   built only when difficulty == 0
#     lid  9 deptstr  (Looningdale's)  built only when difficulty == 0
#     lid  3, 14, 15                   always built
#     everything else                  never built
#
#     002B8EB0  lui   v0, 0x0040
#     002B8EB4  lw    v1, -0xD1C(v0)     0x003FF2E4, the difficulty
#     002B8EB8  b     0x002B8EC4
#     002B8EBC  sltiu a1, v1, 0x1        delay slot: a1 = (difficulty == 0)
#     002B8EC0  addiu a1, zero, 0x1      the "always" levels arrive here
#     002B8EC4  beq   a1, zero, ...      a1 == 0 -> build nothing
#
# So the game's own off switch is a1, and forcing it to zero in that delay
# slot is exactly what Advanced and Expert do. One word, the two levels that
# have the gate, and nothing else -- the "always" levels enter at 0x002B8EC0
# and never execute it.
#
# daddu a1,zero,zero is the game's own idiom for this; the same word sits at
# 0x002B8E64 in this very function.
PROMPT_GATE_AT = 0x002B8EBC
PROMPT_GATE_ORIGINAL = 0x2C650001        # sltiu a1, v1, 0x1
PROMPT_GATE_PATCH = 0x0000282D           # daddu a1, zero, zero

# The intro camera flythrough, for completeness. NOT difficulty gated -- there
# is no difficulty test anywhere on its path. Its lines come from a table at
# 0x00474170 (stride 0x24, indexed by level id; safari's row is string indices
# 50..57) and the sequence is skipped entirely when this counter is zero,
# which is the path every hub and boss already takes. Writing 0 here after a
# level loads silences it; the count is rebuilt on every load.
INTROCAM_COUNT = 0x0046DD88

BONUS_GATE = 0x0021C8B8
BONUS_PATCH_AT = 0x0021C8F4
BONUS_RET_ZERO = 0x0021C9F4
BONUS_RET_ONE = 0x0021CA24
BONUS_TABLE = 0x01F00A00      # free scratch, above notify's code and below its
                              # text buffer at 0x01F01000
BONUS_GATE_GIVE_UP = 5        # failed installs before it stops trying

# Bonus level id -> the level it belongs to. Read out of the jump table at
# 0x004A16E0 and each switch body's load displacement, not from a name.
BONUS_LEVEL = {21: 10, 22: 16, 23: 9, 24: 6, 25: 11,
               26: 14, 27: 4, 28: 15, 29: 5}
BONUS_FIRST_ID = min(BONUS_LEVEL)
# The table's nine bytes, in id order, as the level each one answers for.
BONUS_TABLE_ORDER = [BONUS_LEVEL[i]
                     for i in range(BONUS_FIRST_ID, max(BONUS_LEVEL) + 1)]

# What the shipping game has at BONUS_PATCH_AT: lui/sll/addiu/addu/lw/jr/nop,
# the jump table dispatch.
BONUS_ORIGINAL = [0x3C02004A, 0x00031880, 0x244216E0, 0x00621821,
                  0x8C640000, 0x00800008, 0x00000000]


def _bonus_patch_words():
    """Assemble the seven words, so a wrong branch is an exception here
    rather than a jump into the middle of something in Caleb's game."""
    def rel(at, target):
        off = (target - (at + 4)) // 4
        if not -0x8000 <= off < 0x8000 or (target - (at + 4)) % 4:
            raise ValueError(f"0x{at:08X} cannot branch to 0x{target:08X}")
        return off & 0xFFFF

    hi = BONUS_TABLE >> 16
    lo = BONUS_TABLE & 0xFFFF
    if lo & 0x8000:                      # lbu sign-extends its displacement
        raise ValueError("bonus table displacement must be positive")
    at = BONUS_PATCH_AT
    return [
        0x3C020000 | hi,                                  # lui   v0, hi
        0x00431021,                                       # addu  v0, v0, v1
        0x90420000 | lo,                                  # lbu   v0, lo(v0)
        0x10400000 | rel(at + 12, BONUS_RET_ZERO),        # beq   v0, zero, ret0
        0x00000000,                                       # nop
        0x10000000 | rel(at + 20, BONUS_RET_ONE),         # b     ret1
        0x00000000,                                       # nop
    ]


BONUS_PATCH = _bonus_patch_words()


def _read_words(addr, n):
    raw = mem.read_bytes(addr, 4 * n)
    return [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(n)]


class Game:
    """A live connection to the running game."""

    def __init__(self):
        self.connected = False
        self.save_file = 0
        self.gates = {}
        self.exits = {}
        self._catchers = None
        self._catcher_level = None
        self._enemy_total = None
        self._catcher_posts = {}
        self._despawned = set()
        self.catcher_why = []
        self.catcher_lost = []
        self.catcher_blind = []
        self.despawn_seen = []
        self._death_level = None
        self._settled_at = 0.0
        self._helmet_seen = None
        self._on_last_hit = False
        self._armed = False
        self._phantom_level = None
        self._spoofed = False
        self._true_sandwiches = {}
        # Where Taz last was while the game was actually running. This is what
        # a load is judged by -- see sandwich_tick -- so it must exist from the
        # first tick even if that tick lands mid-load.
        self._last_active_lid = None
        self._hub_anchor = None
        self._bonus_gate_fails = 0
        self._prompt_gate_fails = 0
        # Set by bonus_gate_tick, read by sandwich_tick. False until something
        # says otherwise, so anything that drives sandwich_tick on its own gets
        # the spoof -- which is the safe direction to be wrong in.
        self._bonus_gate_live = False
        # Which save file the honest sweep has been done for, so it happens
        # once rather than ten reads of 480 bytes every tick.
        self._gate_swept = None
        # The head start from the yaml, which lives in the count field only.
        # Set by seed_sandwiches, and needed by true_sandwiches even on a
        # session where seeding had nothing left to do.
        self.starting_sandwiches = 0
        self._true_complete = {}
        self._complete_wrote = {}
        self._complete_level = None
        self._last_pos = None
        self._last_state = None
        self._last_level = None
        self._self_move_until = 0.0
        self._last_state_death = (None, None)
        # When Taz left the cart, and how long effects stay held afterwards.
        # Zero means neither is armed.
        self._coaster_left = 0.0
        self._coaster_until = 0.0
        self._transform_left = 0.0
        self._last_state_obj = None
        self._net_at = 0.0

    # ---------------------------------------------------------------- basics

    def connect(self):
        if mem is None:
            return False
        self.connected = bool(mem.hook())
        return self.connected

    def alive(self):
        if mem is None:
            return False
        if not mem.is_hooked():
            self.connected = bool(mem.hook())
        return self.connected

    def level_id(self):
        try:
            return mem.read_u32(T.LEVEL_ID)
        except Exception:
            return None

    def game_state(self):
        try:
            return mem.read_u32(T.GAME_STATE)
        except Exception:
            return None

    def in_world(self):
        """True once a save file is loaded.

        Before that the save region holds stale bytes and writing to it
        corrupts the file -- the game bounces you back to the title screen.
        Nothing is written until this is true.
        """
        lid = self.level_id()
        return lid is not None and lid >= FIRST_INWORLD_LEVEL_ID

    def demo_running(self):
        """The attract-mode demo plays real levels by itself.

        Left idle on the title screen the game starts a demo, Taz collects
        things, and a client that trusted the save file would hand out
        locations nobody played.
        """
        try:
            return mem.read_u32(T.DEMO_MODE) == 1
        except Exception:
            return False

    def ready(self):
        return self.alive() and self.in_world() and not self.demo_running()

    # The client asks the GAME these, not TazPS2. Putting them only on
    # TazPS2 crashed every connect with AttributeError, and the test did not
    # catch it because it exercised the prompt against a stub game that
    # happened to have the method. Same shape as FakeMem missing read_float:
    # the fake was ahead of the real object, so the suite stayed green.
    FILE_DISPLAY_BASE = 1

    def file_selected(self):
        """0, 1 or 2 -- or None when no save file has been chosen yet.

        None covers the Choose Language screen, the title screen and the
        file-select screen, and it covers a memory read that failed. All of
        those mean the same thing to a caller: do not hand anything over.
        """
        try:
            return T.TazPS2(mem).file_selected()
        except Exception:
            return None

    def file_display(self):
        """What to call the loaded file when talking to the player."""
        f = self.file_selected()
        return None if f is None else f + self.FILE_DISPLAY_BASE

    def refresh_save_file(self):
        try:
            self.save_file = T.TazPS2(mem).save_file()
        except Exception:
            pass
        return self.save_file

    def difficulty(self):
        try:
            return DIFFICULTY_NAMES.get(mem.read_u32(T.DIFFICULTY))
        except Exception:
            return None

    # ---------------------------------------------------------------- reading

    def _u32(self, addr):
        try:
            return mem.read_u32(addr)
        except Exception:
            return 0

    def poster_count(self):
        n = 0
        for lid in D.LEVEL_ORDER:
            base = T.level_block(lid, self.save_file)
            for i in range(D.POSTERS_PER_LEVEL):
                if self._u32(base + T.L_POSTER + i * 4):
                    n += 1
        return n

    def satisfied(self, locations, catcher_kills=()):
        """Which of `locations` the save file currently shows as done.

        Only the level Taz is standing in is evaluated. That is not an
        optimisation -- it is what makes the readings trustworthy:

          * Sandwich counts are deliberately faked while loading and in hubs,
            so that a granted bonus game appears unlocked. Reading them
            anywhere else fired the 100-sandwich check the moment a bonus
            unlock arrived, for a level the player had barely entered.
          * Nothing can be earned in a level you are not in, so there is
            nothing to miss by waiting.

        Inside a level the values are the real ones, because the spoof is put
        back on the way in.
        """
        here = self.level_id()
        out = set()
        # One read per ADDRESS, not one per location.
        #
        # Every destruction threshold in a level shares a single field -- the
        # level's percentage -- so checks every 1% asked PINE for the same
        # word a hundred times a tick. Standing in one level that came to 118
        # round trips per poll against 19 distinct addresses, and at ten polls
        # a second it starved the client's event loop badly enough that the
        # websocket keepalive stopped going out and the server hung up. The
        # emulator froze with it.
        seen = {}

        def read(a):
            if a not in seen:
                seen[a] = self._u32(a)
            return seen[a]

        # Once per LEVEL, not once per threshold. Never the count field: that
        # is the one we spoof, and reading it back is what sent every sandwich
        # check in Cartoon Strip-Mine at once after a resync. true_sandwiches
        # does its own reads, so calling it a hundred times would also undo
        # the round-trip budget the dedup above exists to protect.
        sand = {}

        def sandwiches(lvl):
            if lvl not in sand:
                known = self._true_sandwiches.get(lvl)
                sand[lvl] = (known if known is not None
                             else self.true_sandwiches(lvl))
            return sand[lvl]

        for loc in locations:
            t = loc["type"]
            # Bosses are their own levels, so this covers them too.
            #
            # Bonus games are the exception, and it is why they never sent: the
            # portal is in the HUB, so the player is standing in level 3, 8 or
            # 13 when one is completed and never in the level the check belongs
            # to. The reason for the gate is the sandwich spoof, and the bonus
            # flag is only ever read here, never written, so it is safe to read
            # from anywhere.
            if (t != "bonus" and loc.get("level") is not None
                    and loc["level"] != here):
                continue
            if t == "catcher":
                if (loc["level"], loc["index"]) in catcher_kills:
                    out.add(loc["id"])
                continue
            if t == "completion":
                # NOT from the field, ever. A completion location's rule is
                # "the flag at +0x000 is non-zero", and this generic reader
                # honoured it -- so every fix that stopped read_completions
                # trusting that flag left this path still trusting it, and the
                # check kept firing the moment a poster gate wrote the flag.
                #
                # Three levels, three rounds of this, and each time only half
                # the leak was closed. Completion comes from Client.completed,
                # which is fed by read_completions: seven posters and the exit.
                continue
            addr = D.location_address(loc, self.save_file)
            if addr is None:
                continue
            if t == "sandwich":
                if sandwiches(loc["level"]) >= loc["threshold"]:
                    out.add(loc["id"])
            elif loc["rule"] == "at_least":
                if read(addr) >= loc["threshold"]:
                    out.add(loc["id"])
            elif read(addr):
                out.add(loc["id"])
        return out

    # ---------------------------------------------------------------- writing

    def _w32(self, addr, value):
        try:
            if mem.read_u32(addr) != value:
                mem.write_u32(addr, value)
                return True
        except Exception:
            pass
        return False

    def read_completions(self):
        """Whether the level Taz is standing in reads as complete.

        Only that one, and only from the player's own data: all seven wanted
        posters destroyed, and Taz standing where the level ends. Both are
        things the client never writes.
        """
        lid = self.level_id()
        if lid not in D.LEVEL_ORDER:
            return set()

        # All seven posters AND standing at the spot that ends the level.
        # That is what the game itself asks for, and it is the ONLY thing
        # asked here.
        #
        # THE COMPLETION FLAG IS NOT EVIDENCE AND IS NOT CONSULTED.
        #
        # It reads like the obvious answer and it is not, because the client
        # writes it in both modes -- enforce_access uses those three flags as
        # the boss gate in Open, enforce_linear_gate does the same from the
        # poster count -- so the field is as likely to be ours as theirs.
        #
        # It was kept as a fallback behind a remembered "the player's own
        # value", captured before the first write. That failed three times,
        # each time on a different level, and the third one is why the whole
        # idea is gone:
        #
        #   Cartoon Strip-Mine  read the field directly. Beating Dodge City
        #                       wrote all three of its hub's flags and walking
        #                       in cashed one of them.
        #   Looningdale's       enforce_linear_gate did the same and recorded
        #                       nothing, so there was no remembered value to
        #                       prefer.
        #   Granny Canyon       the remembered value was captured honestly and
        #                       was STILL ours -- a 1 left in the save file by
        #                       an earlier session. Nothing readable at run
        #                       time can tell that apart from a completion the
        #                       player earned, because it is the same byte.
        #
        # The posters and the exit are the player's own data end to end. They
        # cost a return trip for a level finished before the client ever ran,
        # which is a fair price for never sending a check nobody earned.
        if self.posters_done(lid) and self.at_level_exit(lid):
            return {lid}
        return set()

    def enforce_access(self, levels, bosses, mode="open"):
        """Hold the world in whatever state the client says it should be.

        Almost none of this applies to Linear. The game does its own locking
        there, so forcing the warp doors, the hub access fields and the boss
        completions -- as this used to, unconditionally -- opened the entire
        game at once.

        What both modes share is only the quality-of-life pair: the tutorial
        skip, and Zooney Tunes marked as a hub so that hub 1's other two doors
        exist at all. Without the second, the doors to Ice Burg and Looney
        Lagoon are simply not there.
        """
        changed = []
        f = self.save_file

        # Shared. The tutorial popups are a chore on a repeat playthrough.
        try:
            a = TUTORIAL_FLAG + f * T.FILE_STRIDE
            v = mem.read_u32(a)
            if v & TUTORIAL_BIT:
                mem.write_u32(a, v & ~TUTORIAL_BIT)
        except Exception:
            pass

        # Shared. Hub 1 derives its second and third doors from this one.
        self._w32(T.access_addr(5, f), OPEN_VALUE.get(5, T.ACCESS_LEVEL))

        if mode != "open":
            # Actively closed, not merely left alone. It is a session global,
            # so a previous Open-mode run in the same session leaves it set
            # and every warp door stays open.
            self._w32(T.WARP_DOORS_OPEN, 0)
            self._linear_access(f, levels)
            return changed

        # Everything below is Open only.
        self._w32(T.WARP_DOORS_OPEN, 1)

        for hub in HUBS:
            self._w32(T.access_addr(hub, f), T.ACCESS_HUB)

        for lid in D.LEVEL_ORDER:
            if lid in ALWAYS_OPEN:
                self._w32(T.access_addr(lid, f),
                          OPEN_VALUE.get(lid, T.ACCESS_LEVEL))
                continue
            want = (OPEN_VALUE.get(lid, T.ACCESS_LEVEL)
                    if lid in levels else T.ACCESS_LOCKED)
            if self._w32(T.access_addr(lid, f), want):
                changed.append(f"{D.LEVEL_NAME[lid]} "
                               f"{'unlocked' if want else 'locked'}")

        for bid in sorted(BOSS_ACCESS):
            self._w32(T.access_addr(bid, f),
                      T.ACCESS_HUB if bid in bosses else T.ACCESS_LOCKED)

        # The three level-complete flags ARE the boss gate -- but the gate is
        # only ever read in a hub, so they are only written there. Writing them
        # while the player is inside a level destroyed the one signal that says
        # whether they actually finished it.
        #
        # Linear manages the same flags from the poster count instead, in
        # enforce_linear_gate, and must not have them written from here.
        if self.level_id() in HUBS:
            for hub, (lvls, boss) in HUB_GROUPS.items():
                want = 1 if boss in bosses else 0
                for lid in lvls:
                    a = T.level_block(lid, f) + T.L_COMPLETE
                    # Whatever is there before the FIRST write is the player's
                    # own answer. After that it is ours, and saying so is what
                    # lets completion_tick tell them apart later.
                    if lid not in self._true_complete:
                        self._true_complete[lid] = self._u32(a)
                    self._w32(a, want)
                    self._complete_wrote[lid] = want
        return changed

    def completion_tick(self):
        """Put a level's completion flag back to the player's own value.

        Mirrors the sandwiches: the client writes this field for its own
        reasons, so what the field says on arrival is not evidence about the
        player. Remember theirs, restore it on the way in, and keep learning
        it while they are actually in there -- a genuine completion still sets
        the flag and still gets picked up.
        """
        lid = self.level_id()
        if self.game_state() != STATE_ACTIVE or lid not in D.LEVEL_ORDER:
            return []
        a = T.level_block(lid, self.save_file) + T.L_COMPLETE
        v = self._u32(a)
        if lid != self._complete_level:
            self._complete_level = lid
            if self._complete_wrote.get(lid) == v:
                # Still holding exactly what we put there, so it is ours.
                v = self._true_complete.get(lid, 0)
                self._w32(a, v)
        self._true_complete[lid] = v
        return []

    # Which hub each level belongs to, and the boss whose gate opens it.
    LINEAR_STAGES = [(None, [4, 5, 6]), (7, [9, 10, 11]),
                     (12, [14, 15, 16]), (17, [18])]

    def _linear_access(self, f, open_bosses):
        """Put the access fields back to what Linear expects.

        The fields live in the SAVE FILE, so a file that an Open-mode session
        unlocked stays unlocked when the same file is used for a Linear seed --
        the game has no reason to close them again. Writing them from the
        poster gates repairs that, and matches what the game would have done on
        its own.

        Hub 1 is always open; each later hub follows the gate before it.
        """
        for boss, lvls in self.LINEAR_STAGES:
            want = (T.ACCESS_LEVEL if boss is None or boss in open_bosses
                    else T.ACCESS_LOCKED)
            for lid in lvls:
                if lid in ALWAYS_OPEN:
                    continue
                self._w32(T.access_addr(lid, f),
                          OPEN_VALUE.get(lid, want) if want else want)

    def enforce_linear_gate(self, open_bosses):
        """Open a boss by marking its hub's levels complete.

        Two things must not happen, and this used to do the second one every
        tick:

        * The flags must not change while the player is STANDING IN the hub
          that reads them. The door would change under them and the gate text
          is built on load, so it would be out of step.

        * A level's flag must not be written while the player is INSIDE that
          level. It is the only thing that says whether they finished it, and
          we would be writing over the answer while they were producing it --
          which sent Looningdale's Level Complete the moment they walked in,
          on a seed whose Gladiatoons poster gate was already met.

        A load is neither: the player is not playing the level they are
        leaving, so that is when its flag is safe to set, and it is set well
        before the hub that reads it is built.

        And whatever is there before the FIRST write is the player's own
        answer, recorded here so completion_tick and read_completions can tell
        it apart from ours later. enforce_access does exactly this for Open;
        this did none of it.
        """
        lid = self.level_id()
        active = self.game_state() == STATE_ACTIVE
        if active and lid in HUBS:
            return []
        playing = lid if active else None
        out = []
        f = self.save_file
        for hub, (lvls, boss) in HUB_GROUPS.items():
            want = 1 if boss in open_bosses else 0
            for target in lvls:
                if target == playing:
                    continue
                a = T.level_block(target, f) + T.L_COMPLETE
                if target not in self._true_complete:
                    self._true_complete[target] = self._u32(a)
                if self._w32(a, want):
                    out.append(f"{D.LEVEL_NAME[target]} complete={want}")
                self._complete_wrote[target] = want
        return out

    def enforce_costumes(self, granted_costumes):
        """Block a level's phone booth unless its costume has been granted."""
        lid = self.level_id()
        cid = LEVEL_COSTUME.get(lid)
        addr = mem.deref(T.TAZ_PTR, T.O_COSTUME_PTR, T.C_COSTUME) \
            if mem else None
        if addr is None:
            return None
        try:
            cur = mem.read_u8(addr)
        except Exception:
            return None

        if cid is None or cid in granted_costumes:
            # Clear a phantom left from somewhere else. Checking the byte
            # rather than our own bookkeeping matters: a level change resets
            # the flag but not the memory.
            if cur == PHANTOM_COSTUME:
                try:
                    mem.write_u8(addr, T.COSTUME_NONE)
                except Exception:
                    pass
                return "booth released"
            return None

        if cur != PHANTOM_COSTUME:
            try:
                mem.write_u8(addr, PHANTOM_COSTUME)
            except Exception:
                return None
            if self._phantom_level != lid:
                self._phantom_level = lid
                return f"{D.LEVEL_NAME.get(lid, lid)} booth locked"
        return None

    # ------------------------------------------------------------ sandwiches

    def sandwich_tick(self, granted_bonus):
        """Make a granted bonus game appear without ever hiding a sandwich.

        Two pieces of the game's own code decide everything here. Both were
        read out of the RAM dump rather than reasoned about, and they do not
        agree with what this code used to assume:

        0x0021C8B8   the police box gate. A hub builds a bonus game portal
                     only if the matching level's count reads AT LEAST 100 --
                     `slti v1,v1,0x64` at 0x0021C9EC, a less-than test, so
                     anything from 100 up passes. It runs once, while the
                     hub's map is being constructed, which is during the
                     loading screen.

        0x0024A6D8   the collectible update. While a level is RUNNING, if that
                     level's count reads EXACTLY 100 -- `bnel v1,t4` with t4
                     held at 0x64 -- every sandwich object in it is sent
                     'destroycollect'. Every frame, not once at load.

        Three things follow, and they are the whole design:

        * The despawn was never a load-time race that could be repaired a
          tenth of a second later. One frame at exactly 100 while the level is
          running empties it. Writing 100 and correcting it afterwards could
          not have worked, at any poll rate.

        * 100 is not the only number that opens a portal, but it is the only
          one that empties a level. So the spoof is 101. The portal is still
          built, the destroyer never matches, and the two requirements stop
          fighting each other. SPOOF_COUNT, at the top of this file.

        * The remaining question is only which direction a load is going, and
          it is answered from where the load STARTED rather than from lid --
          which still reads as the hub for the first frames after walking out
          of one, and is what made this flicker.

        101 is also a number the game cannot write, so finding one in a save
        is proof it is ours. That is what stops a reconnect reading its own
        spoof back as a hundred real sandwiches.

        What is left is honesty: the count is what a level entrance displays,
        so the real number goes back once Taz is standing in the hub that owns
        that level.

        ALL OF WHICH IS THE FALLBACK NOW. The gate at 0x0021C8B8 is patched to
        read the granted list instead of the count (see BONUS_GATE), and while
        that patch is live none of this runs -- every count is simply the
        truth, always. It stays here because PCSX2 can keep running an older
        translation of a patched block, and because a future build might not
        match those addresses. If the patch is not in, this is.
        """
        lid = self.level_id()
        gs = self.game_state()
        f = self.save_file
        out = []

        if self._bonus_gate_live:
            # Nothing below this line is needed while the gate is patched. The
            # count decides nothing, so there is nothing to lie about: every
            # level reads the player's real number in every state, including
            # a level they genuinely finished without being granted its bonus.
            #
            # One sweep per save file, not one per tick. Whatever an earlier
            # session or an unpatched moment left behind is put right once,
            # and after that the game keeps its own counts and this writes
            # nothing at all.
            self._hub_anchor = None
            if gs == T.STATE_ACTIVE and lid is not None:
                self._last_active_lid = lid
            if self._spoofed or self._gate_swept != f:
                out += self._write_true(f)
                self._gate_swept = f
            if gs == T.STATE_ACTIVE and lid in D.LEVEL_ORDER:
                self._true_sandwiches[lid] = self.true_sandwiches(lid, f)
            return out

        # The gate is not patched -- an old PCSX2 translation of the block, a
        # build whose addresses do not match, a tool driving this on its own.
        # Everything from here down is the count-based spoof, which is what
        # made the portals appear before the patch existed and still does.
        self._gate_swept = None

        if gs == T.STATE_ACTIVE and lid in D.LEVEL_ORDER:
            self._hub_anchor = None
            self._last_active_lid = lid
            if self._spoofed:
                out += self._write_true(f)
            # The bitmap in preference to the count, for the same reason.
            true = self.true_sandwiches(lid, f)
            self._true_sandwiches[lid] = true
            # The one write that is never skipped, whatever else did or did
            # not happen. The level Taz is standing in reads the real number,
            # because a running level whose count is exactly 100 destroys
            # every sandwich in itself (0x0024A6D8).
            if lid not in D.NO_BONUS:
                self._w32(T.level_block(lid, f) + T.L_SANDWICHES, true)
            return out

        if gs == T.STATE_ACTIVE and lid in HUBS:
            self._last_active_lid = lid
            # The spoof has to outlive the load. The portal is built a moment
            # AFTER the hub becomes playable, so putting the true counts back
            # the instant it does meant the portal was never there.
            #
            # Movement is the cheap proof that everything which reads the
            # count has read it. Until then the spoof stays and the entrance
            # is briefly wrong; after it the real numbers go back.
            #
            # Every level, not just this hub's three: standing still is the
            # one moment nothing is reading these numbers for a decision, so
            # it costs nothing to be completely honest. The spoof is put back
            # by the load itself, whichever way it goes.
            if self._hub_settled(lid):
                out += self._write_true(f)
            return out

        if gs in LOAD_STATES:
            self._hub_anchor = None
            # Judged by where the load STARTED, not by lid.
            #
            # lid does not become the destination when the loading screen
            # appears: walking out of a hub it still reads as the hub for the
            # first frames. Every version of this that consulted lid during a
            # load -- latched on the first tick or re-evaluated on all of them
            # -- wrote the spoof into the level being entered, and a level
            # holding exactly 100 destroys its own sandwiches on the next
            # frame it runs. That is Ice Burg, emptied.
            #
            # _last_active_lid is known from the first tick of the load and
            # does not change during it, so this decides once and never
            # flickers.
            src = self._last_active_lid
            if src in HUBS:
                # Leaving a hub. The destination is one of its own three
                # levels, or another hub. Those three keep the truth -- 100 in
                # the one being entered would empty it -- and everything else
                # keeps the spoof, which is what a hub reached from here needs.
                out += self._write_hub(f, granted_bonus,
                                       true_for=D.HUB_LEVELS.get(src, ()))
            elif src is not None and src >= FIRST_INWORLD_LEVEL_ID:
                # Leaving a level, a boss or a bonus game: a hub is coming up
                # and it needs EVERY portal it is owed -- including the one
                # for the level just left. Holding that one at its true count
                # is why the bonus game vanished on the way out. Nothing here
                # can be walked into, so nothing needs the truth.
                out += self._write_hub(f, granted_bonus, true_for=())
            # Any other source -- a client that connected mid-load, the title
            # screen -- writes nothing. Not knowing where this is going, the
            # only move that cannot cost somebody a level's sandwiches is not
            # to move. A portal missed once comes back on the next trip.
        return out

    # ------------------------------------------------- the boss door panel

    def boss_door_text(self, boss_id, text=None):
        """Put `text` on every line of a boss door, or None for the game's own.

        Every line, because the game picks between five and the one it picks
        depends on which level the player was in last -- so anything that
        writes one of them is writing the wrong one most of the time.

        Idempotent and cheap: five pointer reads, and a write only when
        something actually differs. Safe to call every tick, which it needs to
        be, because the panel is rebuilt from the table each time the player
        walks into the door.
        """
        door = BOSS_DOOR[boss_id]
        out = []
        if text is not None:
            if len(text) > DOOR_TEXT_CAP:
                raise ValueError(f"{text!r} is {len(text)} characters, "
                                 f"capacity {DOOR_TEXT_CAP}")
            buf = (DOOR_TEXT_BUF
                   + sorted(BOSS_DOOR).index(boss_id) * DOOR_TEXT_SLOT)
            raw = text.encode("utf-16-le") + b"\0\0"
            if mem.read_bytes(buf, len(raw)) != raw:
                mem.write_bytes(buf, raw)
        for idx, orig_ptr, orig_len in door["lines"]:
            entry = STR_TABLE + idx * STR_STRIDE
            live = self._u32(entry)
            if live != orig_ptr and not (DOOR_SCRATCH_LO <= live
                                         < DOOR_SCRATCH_HI):
                # Neither the shipping string nor one of ours. Something else
                # owns this entry and overwriting it would lose the only copy
                # of where the real text lives.
                raise RuntimeError(
                    f"string {idx} points at 0x{live:08X}, which is neither "
                    f"the shipping 0x{orig_ptr:08X} nor our scratch")
            want_ptr = buf if text is not None else orig_ptr
            want_len = len(text) if text is not None else orig_len
            # Silent. This happens on every hub the player walks into and
            # again whenever the game rebuilds the panel; there is no decision
            # for them to make about it.
            if live != want_ptr:
                self._w32(entry, want_ptr)
            # Nothing in the game reads this, but leaving it wrong would
            # mislead the next person who does.
            if self._u32(entry + 4) != want_len:
                self._w32(entry + 4, want_len)
        return out

    def boss_door_restore_all(self):
        """Every door back to the shipping text. Never raises."""
        for boss_id in BOSS_DOOR:
            try:
                self.boss_door_text(boss_id, None)
            except Exception:
                pass

    # -------------------------------------------- the Standard-only prompts

    def prompt_gate_installed(self):
        try:
            return self._u32(PROMPT_GATE_AT) == PROMPT_GATE_PATCH
        except Exception:
            return False

    def prompt_gate_set(self, off):
        """Silence the Standard-only prompt book, or hand it back.

        `off` True makes Zooney Tunes and Looningdale's behave on Standard
        exactly as they already do on Advanced and Expert. Nothing else in the
        game reaches this instruction.

        Refuses anything that is neither the shipping word nor ours, which is
        what stops a build whose addresses do not match being patched on a
        guess. Never raises; a refusal leaves the game as it shipped.
        """
        want = PROMPT_GATE_PATCH if off else PROMPT_GATE_ORIGINAL
        try:
            live = self._u32(PROMPT_GATE_AT)
            if live not in (PROMPT_GATE_ORIGINAL, PROMPT_GATE_PATCH):
                self._prompt_gate_fails += 1
                if self._prompt_gate_fails == BONUS_GATE_GIVE_UP:
                    return [f"0x{PROMPT_GATE_AT:08X} holds 0x{live:08X}, "
                            f"neither the shipping prompt gate nor ours -- "
                            f"leaving the hints alone"]
                return []
            self._prompt_gate_fails = 0
            if live != want:
                self._w32(PROMPT_GATE_AT, want)
        except Exception:
            pass
        return []

    # ------------------------------------------------ the bonus game gate

    def bonus_gate_installed(self):
        """Is our patch in the police box gate right now?"""
        try:
            return _read_words(BONUS_PATCH_AT, len(BONUS_PATCH)) == BONUS_PATCH
        except Exception:
            return False

    def bonus_gate_original(self):
        """Is the gate the untouched shipping code?"""
        try:
            return (_read_words(BONUS_PATCH_AT, len(BONUS_ORIGINAL))
                    == BONUS_ORIGINAL)
        except Exception:
            return False

    def bonus_gate_write_table(self, granted_bonus):
        """One byte per bonus game, 1 where the server has granted it.

        Written before the patch goes in and re-asserted every tick, because
        the table IS the answer -- an all-zero table with the patch installed
        is nine portals that never appear.
        """
        want = bytes(1 if lid in granted_bonus else 0
                     for lid in BONUS_TABLE_ORDER)
        if mem.read_bytes(BONUS_TABLE, len(want)) != want:
            mem.write_bytes(BONUS_TABLE, want)
            return True
        return False

    def bonus_gate_install(self, granted_bonus):
        """Repoint the gate at our table. Idempotent.

        Refuses unless the seven words are either the shipping ones or our
        own. Anything else means the addresses are wrong for this build, and
        patching on a guess is a crash rather than a missing portal.
        """
        if not (self.bonus_gate_installed() or self.bonus_gate_original()):
            got = _read_words(BONUS_PATCH_AT, len(BONUS_PATCH))
            raise RuntimeError(
                f"0x{BONUS_PATCH_AT:08X} holds "
                + " ".join(f"{w:08X}" for w in got)
                + " -- neither the shipping gate nor ours, so not touching it")
        # Table first. The patch reads it on the very next map load, and a
        # gap between the two is a hub built from uninitialised scratch.
        self.bonus_gate_write_table(granted_bonus)
        mem.write_bytes(BONUS_PATCH_AT,
                        b"".join(struct.pack("<I", w) for w in BONUS_PATCH))
        if not self.bonus_gate_installed():
            raise RuntimeError("the bonus gate patch did not stay written")
        return True

    def bonus_gate_remove(self):
        """Put the shipping seven words back."""
        try:
            if self.bonus_gate_installed():
                mem.write_bytes(
                    BONUS_PATCH_AT,
                    b"".join(struct.pack("<I", w) for w in BONUS_ORIGINAL))
            return self.bonus_gate_original()
        except Exception:
            return False

    def bonus_gate_tick(self, granted_bonus):
        """Keep the gate patched and the table current. Cheap; every tick.

        Never raises. A gate that cannot be patched leaves the game exactly as
        it shipped, and the sandwich spoof underneath still makes the portals
        appear -- so the worst case is the behaviour we had before the patch
        existed, not a broken client.
        """
        if mem is None:
            self._bonus_gate_live = False
            return []
        try:
            if self.bonus_gate_installed():
                self.bonus_gate_write_table(granted_bonus)
                self._bonus_gate_fails = 0
                self._bonus_gate_live = True
                return []
            self._bonus_gate_live = False
            if self._bonus_gate_fails >= BONUS_GATE_GIVE_UP:
                return []
            self.bonus_gate_install(granted_bonus)
            self._bonus_gate_fails = 0
            self._bonus_gate_live = True
            # Normally nothing to say: this happens on every reload and the
            # player has no decision to make about it.
            #
            # But installing it only NOW, with a map already up, means that
            # map's police boxes were built by the shipping gate -- which
            # answers from the sandwich count, not from the server. The patch
            # decides construction and cannot un-build anything, so say so
            # and name the one thing that fixes it.
            if self.in_world():
                return ["bonus gate patched late -- any police box already "
                        "standing here was built from the sandwich count, "
                        "not from what you have been granted. Leave and "
                        "re-enter to rebuild them."]
            return []
        except Exception as exc:
            self._bonus_gate_fails += 1
            self._bonus_gate_live = False
            if self._bonus_gate_fails == BONUS_GATE_GIVE_UP:
                return [f"bonus gate patch not applied ({exc}) -- falling back "
                        f"to the sandwich count, which still works"]
            return []

    def _hub_settled(self, lid):
        """Has Taz actually started moving in this hub?

        Anchored on the position first seen while the hub is active, and
        re-anchored whenever the hub is left, so walking out to a level and
        back is a fresh wait rather than an already-satisfied one.

        The minimum dwell covers a player already running as the hub comes up;
        the ceiling covers one who puts the controller down, so an idle player
        does not stare at a wrong sandwich count forever.
        """
        pos = self._pos()
        if getattr(self, "_hub_anchor", None) is None:
            self._hub_anchor = pos
            self._hub_anchor_at = time.time()
            return False
        waited = time.time() - getattr(self, "_hub_anchor_at", 0.0)
        if pos is None or self._hub_anchor is None:
            return waited >= 10.0
        moved = any(abs(a - b) > 1.0 for a, b in zip(pos, self._hub_anchor))
        return (moved and waited >= 1.0) or waited >= 10.0

    def _bitmap_count(self, lid, f=None):
        """How many sandwiches were physically PICKED UP. Not the true count.

        Call true_sandwiches instead -- always. This number is missing the
        head start, and writing it anywhere is how nine levels lost twenty-five
        sandwiches each. taz_sandwich_test.py asserts that this has exactly one
        caller, which is the only reason that cannot happen twice.

        The count at +0x1E4 is the field we spoof, so reading it back can hand
        us our own lie -- which is exactly what happened on a resync: a fresh
        client found 100 sitting in Cartoon Strip-Mine, believed it, and fired
        all hundred sandwich checks the moment the player walked in.

        +0x004 is 480 bytes, one dword per sandwich, and the client never
        writes a byte of it -- so it is the player's own record and cannot be
        one of ours. It is also only what they TOUCHED, which is why it is not
        the answer on its own.

        Returns None if it does not look like what it is supposed to be -- a
        malformed read must fall back rather than write a wrong count over
        somebody's progress.
        """
        if not mem:
            return None
        base = T.level_block(lid, self.save_file if f is None else f)
        try:
            raw = mem.read_bytes(base + T.L_SANDWICH_BITS, 480)
        except Exception:
            return None
        if len(raw) < 480:
            return None
        words = struct.unpack("<120I", raw[:480])
        if any(w not in (0, 1) for w in words):
            return None
        n = sum(words)
        return n if 0 <= n <= SANDWICH_GOAL else None

    def true_sandwiches(self, lid, f=None):
        """The player's real count for a level.

        Two fields claim to know, and neither is safe on its own:

          +0x1E4  the count. We spoof it -- but only ever UP to exactly 100,
                  or down to the 99 cap. It is never some other wrong number.
          +0x004  one dword per sandwich, which we never write. Right, unless
                  the read itself failed -- and a failed read here comes back
                  as ZERO, which is indistinguishable from a fresh level.

        So each one covers the other's failure. The bitmap wins when it is at
        least what the count says, or when the count is exactly 100 and
        therefore ours. Otherwise the count stands: a bitmap BELOW a count we
        never inflate is a bitmap that was not read properly, and writing that
        back would delete real progress -- which is the one mistake here that
        cannot be undone, because sandwiches do not respawn.
        """
        live = self._u32(T.level_block(lid, self.save_file if f is None else f)
                         + T.L_SANDWICHES)
        if not 0 <= live <= SANDWICH_GOAL:
            # Above a hundred is a number the GAME cannot produce -- its own
            # counter stops there -- so it is SPOOF_COUNT and it says nothing
            # whatever about the player. Discarding it is what stops a
            # reconnect reading its own spoof back as real progress.
            live = 0
        n = self._bitmap_count(lid, f)
        if n is not None:
            # PLUS the head start. Starting Sandwiches is given by writing the
            # COUNT field and nothing else, so the bitmap only ever holds what
            # was physically picked up. Measured across all ten levels of a
            # save with a start of 25:
            #
            #   count == bitmap + 25   everywhere it was not spoofed or capped
            #
            # Without this, a level with a granted bonus -- count 100, bitmap
            # 0 -- reads as zero, and writing that back deletes the head start
            # for good.
            n = min(n + self.starting_sandwiches, SANDWICH_GOAL)
            if n >= live or live == SANDWICH_GOAL:
                return n
            # Below a count we never deflate: the bitmap did not read
            # properly, so the count is the better of two bad answers.
        known = self._true_sandwiches.get(lid)
        return known if known is not None else live

    def _write_hub(self, f, granted_bonus, true_for=()):
        """Spoof every bonus level's count, except the ones Taz can reach.

        `true_for` is the levels that could be walked into from wherever Taz
        is right now. They get the real number and nothing else will do: a
        level whose count reads exactly 100 destroys every sandwich object in
        itself while it runs (0x0024A6D8), which is what emptied Cartoon
        Strip-Mine and then Ice Burg.

        Everything outside that set gets SPOOF_COUNT if the server has granted
        its bonus game, and is held one short of 100 if it has not -- so a
        player who genuinely found all hundred still cannot reach a portal
        they were never given.
        """
        out = []
        true_for = set(true_for)
        for lid in D.LEVEL_ORDER:
            if lid in D.NO_BONUS:
                continue
            # Always re-derived rather than trusted from the dict, because
            # the dict can only ever be as good as what put it there -- and on
            # a reconnect what put it there was a count field we had already
            # spoofed. The bitmap has no such problem.
            true = self.true_sandwiches(lid, f)
            self._true_sandwiches[lid] = true
            want = (true if lid in true_for
                    else SPOOF_COUNT if lid in granted_bonus
                    else min(true, SANDWICH_GOAL - 1))
            if self._w32(T.level_block(lid, f) + T.L_SANDWICHES, want):
                out.append(f"{D.LEVEL_NAME[lid]} sandwiches -> {want}")
        self._spoofed = True
        return out

    def _write_true(self, f):
        out = []
        for lid in D.LEVEL_ORDER:
            if lid in D.NO_BONUS:
                continue
            # true_sandwiches, NOT _bitmap_count. This called the bitmap
            # directly and wrote it raw, which is the head start missing from
            # every level at once: nine levels dropped by exactly 25, and only
            # Tazland -- which has no bonus game and is skipped above -- was
            # left alone. Anything that decides a number to WRITE has to go
            # through the one function that knows about the head start.
            true = self.true_sandwiches(lid, f)
            if true is None:
                # Nothing known about this one, so leave the save alone rather
                # than assert a count for it.
                continue
            self._true_sandwiches[lid] = true
            self._w32(T.level_block(lid, f) + T.L_SANDWICHES, true)
        self._spoofed = False
        return out

    def seed_sandwiches(self, starting):
        """Give every level its starting sandwiches, once per file.

        Written into the COUNT and nowhere else -- there is no way to hand
        somebody a sandwich they never touched. That is why true_sandwiches
        has to add it back on top of the bitmap.
        """
        self.starting_sandwiches = int(starting or 0)
        if not starting:
            return
        f = self.save_file
        for lid in D.LEVEL_ORDER:
            a = T.level_block(lid, f) + T.L_SANDWICHES
            if self._u32(a) < starting:
                self._w32(a, starting)
                self._true_sandwiches[lid] = starting

    # ---------------------------------------------------------- boss deaths

    # A boss arena has no level start to send the player back to, so a
    # DeathLink there means losing the fight instead. Each boss loses
    # differently, and the distinction that matters is once-versus-held:
    # pinning a score at the winning number stops the game ever REACHING it,
    # so those are set one short and the boss takes the last point itself.
    BOSS_LOSS = {
        7:  {"fields": [{"addr": 0x0037D8FC, "value": 2, "hold": False}],
             "park": (4000.0, 5.0, -2500.0), "secs": 12.0},
        # Zero Taz and put Daffy ahead, as well as ending the clock -- the
        # fight then LOOKS lost rather than just stopping.
        12: {"fields": [{"addr": 0x008277FC, "value": 0, "hold": True,
                         "size": 1},
                        {"addr": 0x0088376C, "value": 5, "hold": True,
                         "size": 1},
                        {"addr": 0x00380E28, "value": 999.0, "hold": False,
                         "float": True}],
             "park": None, "secs": 12.0},
        19: {"fields": [{"addr": 0x00383EB0, "value": 5, "hold": False}],
             "park": (-2784.0, -1408.0, -11.0), "secs": 20.0},
    }
    # Dodge City and The Hindenbird are fought in a helmet: empty it and hold
    # Taz still so the boss can land the finishing hit. The Hindenbird also
    # needs the bite suppressed, since that is how Taz attacks and a player
    # mashing it can still win while frozen.
    HELMET_BOSSES = {17: False, 20: True}
    HELMET_CHAIN = (0x1D0, 0x0C)
    HELMET_NEXT = (0x1D0, 0x08)
    BITE_STATE = 0x0B

    # Losing a boss fight. This used to be five per-arena readings plus a
    # shared state; it is now one global, and the readings are kept only as
    # a second opinion.
    #
    # bossgamecontinue.cpp (the source path is at 0x0049A738, which is how
    # the module was identified) has one entry point, 0x001A1B08. It news a
    # 60-byte object and parks the pointer at 0x0037FC3C:
    #
    #   0x001A1B70  sw $v0, -0x3c4($s0)      $s0 = 0x00380000
    #
    # Zero the whole rest of the time; non-zero from the losing blow until
    # the "Continue this game" prompt is answered. Five writers in the whole
    # image and no others: 0x001A1B70, 0x001A2550 (destructor), and
    # 0x001A2988 / 0x001A2994 / 0x001A2998 (the finisher 0x001A27B8).
    #
    # 0x001A1B08 has exactly SEVEN callers, all inside the five boss modules
    # and nowhere else -- 0x00197D88 ZooBoss, 0x001A63FC CityBoss,
    # 0x00190E1C WestBoss, 0x001A98DC tazboss1, and 0x0017E204 /
    # 0x00184EA8 / 0x00187E2C for the Hindenbird's three phases. Not normal
    # death, not the bonus games, not two-player. It is polled every frame
    # from the level tick's shared tail (0x00228450), so it is live
    # everywhere.
    #
    # This is why state 0x5A was never enough: 0x00184E44 is the ONLY site
    # in the image that installs it (checked at all 41 install_state call
    # sites), it lives in mtweetymagnet.cpp, and it is gated on Taz wearing
    # a helmet. Gladiatoons has no helmet, so it could never announce a loss
    # that way -- and Dodge City signals through 0x0036BCD0 instead.
    BOSS_CONTINUE_PTR = 0x0037FC3C
    BOSS_CONTINUE_ANSWER = 0x0C     # 0 pending, 1 continue, 2 quit
    # A load in progress can leave rubbish in a global, and a DeathLink sent
    # off rubbish kills everyone else in the multiworld. Only a plausible
    # main-RAM pointer counts.
    BOSS_CONTINUE_LO = 0x00100000
    BOSS_CONTINUE_HI = 0x02000000

    # When WE force a loss to apply a received DeathLink, the game raises its
    # own Continue prompt -- which is indistinguishable from the player
    # losing on their own. Exactly one rising edge is swallowed, and only
    # inside this window, so a real loss straight after a received one still
    # sends.
    FORCED_LOSS_GRACE = 20.0

    # Still load-bearing, but for ONE arena now. Losing to Tweety offers no
    # Continue screen -- recorded, not reasoned -- so level 20 has nothing
    # else to go on. Which is consistent with where 0x5A comes from:
    # 0x00184E44 is the only site in the image that installs it, it sits in
    # mtweetymagnet.cpp, and it is gated on Taz wearing a helmet. It was
    # never the shared signal it was taken for; it is the Hindenbird's.
    BOSS_LOSS_STATES = frozenset({0x5A})

    BOSS_LOSS_WATCH = {
        7:  {"enemy": 0x0037D8FC, "lose_at": 3},
        19: {"enemy": 0x00383EB0, "lose_at": 6},
        # Gladiatoons counts UP to 120 seconds -- it was read as a countdown
        # to zero, which never happened, so the loss never fired. The score
        # addresses are still unknown; until they are found the timer alone
        # decides, which is wrong only in that it also fires on a WIN.
        # Gladiatoons counts UP to 120 seconds. The old Daffy address was
        # wrong, so the loss could never be read; the receive side happened to
        # work anyway because setting the timer to 999 ends the match on its
        # own.
        #
        # "Past the limit AND Daffy ahead" also expresses sudden death without
        # a special case: at 120 all square nothing fires, and the moment
        # Daffy scores he is ahead and it does. If Taz scores first, he never
        # is.
        # Both scores are single BYTES, found with a memory search after
        # three of my scan bands missed them. Reading them as words gave
        # numbers like 286401329 -- the neighbouring bytes, not a score.
        # The scores move every load, so they are reached through a pointer
        # rather than by address. Confirmed across two loads: the pointer at
        # 0x003FF064 shifted with the score and the offset held at 0x678C,
        # while every other candidate's offset changed.
        #
        # Both are single BYTES.
        12: {"timer": 0x00380E28, "timer_end": 120.0,
             "enemy_ptr": (0x003FF064, 0x678C),
             "ours_ptr": None,          # still to be found the same way
             "byte": True},
        # Armed by the helmet, fired by a state. +0x08 reading 0xFF means
        # Taz is on his last hit; taking damage from there is the loss.
        #
        # Neither half works alone: the counters never move again once he is
        # down to one hit, and a damaged state on its own fires every time he
        # is hit at full health.
        17: {"helmet": True, "damaged": {0x15}},
        20: {"helmet": True, "damaged": {0x5A}},
    }

    # The two helmet fights count in the helmet object rather than a global,
    # at the same two fields the boss-loss code zeroes:
    #
    #   +0x08   0xFF means Taz is on his last hit
    #   +0x0C   0 through that last hit; it leaving 0 is the losing blow
    #
    # Requiring +0x08 to have READ 0xFF first is what keeps The Hindenbird
    # honest: Taz regains health between phases, so +0x08 goes 0xFF -> 0x08
    # without +0x0C ever moving, and that must not count as a loss.
    HELMET_LAST_HIT = 0x08
    HELMET_BLOW = 0x0C
    LAST_HIT_VALUE = 0xFF

    def boss_lost(self):
        """Has the player just lost the fight they are in?

        Read rather than inferred: each arena keeps a score, and losing is
        that score reaching the number the boss needs. Reported once, so a
        scoreline sitting at the losing value does not repeat.
        """
        lid = self.level_id()
        if lid not in BOSS_LEVELS:
            self._lost_reported = False
            self._timer_started = False
            self._fight_over = False
            self._continue_up = False
            return False

        # TWO signals, and between them they cover all five arenas. Neither
        # is per-arena arithmetic and neither has to be inferred.
        #
        #   1. the shared Continue prompt   -- 7, 12, 17, 19
        #   2. state 0x5A                   -- 20, and only 20
        #
        # RECORDED, not reasoned: losing to Tweety offers no Continue
        # screen at all, so the Hindenbird never raises the shared signal.
        # That fits what the dump says rather than contradicting it --
        # 0x00184E44 is the only site in the image that installs 0x5A and
        # it lives in mtweetymagnet.cpp, a Hindenbird module. The two
        # signals are complementary: the four arenas that offer a Continue
        # never set 0x5A, and the one that sets 0x5A never offers one.
        #
        # They are still OR-ed behind a single latch rather than switched on
        # level id, so that if a Hindenbird phase ever does raise a prompt
        # (three of Start's seven callers are Hindenbird modules) it cannot
        # send a second DeathLink for the same fight.
        if mem:
            try:
                p = mem.read_u32(self.BOSS_CONTINUE_PTR)
            except Exception:
                return False
            prompt = self.BOSS_CONTINUE_LO <= p < self.BOSS_CONTINUE_HI
            self._continue_up = prompt

            try:
                down = self._state() in self.BOSS_LOSS_STATES
            except Exception:
                down = False

            if not (prompt or down):
                # Nothing is up. Re-arm, and drop a stale grace window so it
                # cannot swallow a later, genuine loss.
                self._lost_reported = False
                if time.time() >= getattr(self, "_forced_loss_until", 0.0):
                    self._forced_loss_until = 0.0
                return False

            if getattr(self, "_lost_reported", False):
                return False            # same fight, already dealt with
            self._lost_reported = True

            # If WE caused this by applying a received DeathLink, swallow it
            # once -- otherwise the player's own client answers their death
            # by killing the whole multiworld. This has to cover both
            # signals: the receive side freezes Taz so the boss lands the
            # finishing hit, which is a real loss, so it raises whichever
            # signal that arena uses.
            if time.time() < getattr(self, "_forced_loss_until", 0.0):
                self._forced_loss_until = 0.0
                return False
            return True

        # Everything from here down is UNREACHABLE whenever `mem` exists,
        # which is always outside a test stub. It is kept because the
        # addresses in BOSS_LOSS_WATCH were expensive to find and are the
        # second opinion if the two signals above ever stop being enough --
        # not because anything calls it.
        spec = self.BOSS_LOSS_WATCH.get(lid)
        if not spec or not mem:
            self._lost_reported = False
            return False
        try:
            lost = False
            if spec.get("helmet"):
                h = self._helmet_obj()
                if h is None:
                    self._armed = False
                    return False

                # A fresh helmet is a fresh fight, so the arming does not
                # carry across one.
                if h != getattr(self, "_helmet_seen", None):
                    self._helmet_seen = h
                    self._armed = False

                on_last = (mem.read_u8(h + self.HELMET_LAST_HIT)
                           == self.LAST_HIT_VALUE)
                if on_last:
                    self._armed = True
                elif not on_last and getattr(self, "_armed", False):
                    # Healed between phases -- the Hindenbird does this, and
                    # it means the last hit is no longer the last hit.
                    self._armed = False

                st = self._state()
                lost = bool(getattr(self, "_armed", False)
                            and st in spec["damaged"])
            elif "lose_at" in spec:
                lost = mem.read_u32(spec["enemy"]) >= spec["lose_at"]
            elif "timer_end" in spec:
                t = struct.unpack("<f", mem.read_bytes(spec["timer"], 4))[0]

                # The clock reads rubbish until the level has loaded, so it
                # is only believed once it has been seen near zero and
                # climbing -- the garbage value was already above the limit
                # and would have fired instantly.
                # A fresh clock means a fresh fight, and clears the "this
                # one is finished" latch.
                if t < 1.0:
                    self._timer_started = True
                    self._fight_over = False
                if not getattr(self, "_timer_started", False):
                    return False

                # Once the result is in, the clock keeps climbing and the
                # scores stay put -- so without this the same loss reported
                # over and over, and reloading the fight sent another.
                if getattr(self, "_fight_over", False):
                    return False

                over = t >= spec["timer_end"]
                if not over:
                    lost = False
                else:
                    e = self._chain(spec.get("enemy_ptr"))
                    o = self._chain(spec.get("ours_ptr"))
                    if e is None or o is None:
                        # Without both scores the result cannot be read, and
                        # guessing is worse than staying quiet: a false loss
                        # kills everyone else in the multiworld.
                        return False
                    read = mem.read_u8 if spec.get("byte") else mem.read_u32
                    lost = read(e) > read(o)
                    self._fight_over = True
            else:
                lost = False
        except Exception:
            return False
        if lost and not getattr(self, "_lost_reported", False):
            self._lost_reported = True
            return True
        if not lost:
            self._lost_reported = False
        return False

    def _chain(self, spec):
        """Resolve a (pointer address, offset) pair to a live address."""
        if not spec or not mem:
            return None
        base, off = spec
        try:
            p = mem.read_u32(base)
        except Exception:
            return None
        return (p + off) if mem.valid_ptr(p) else None

    def _helmet_obj(self):
        """The helmet Taz is fighting in, or None."""
        if not mem:
            return None
        try:
            taz = mem.read_u32(T.TAZ_PTR)
            if not mem.valid_ptr(taz):
                return None
            cos = mem.read_u32(taz + T.O_COSTUME_PTR)
            if not mem.valid_ptr(cos):
                return None
            h = mem.read_u32(cos + self.HELMET_CHAIN[0])
            return h if mem.valid_ptr(h) else None
        except Exception:
            return None

    def hindenbird_beaten(self):
        """Has Tweety actually been beaten?

        The goal is winning that fight, not merely qualifying for it. Without
        this the run completed the moment the requirements were met, so a
        player could finish having never entered the arena.
        """
        if not mem:
            return False
        try:
            return bool(mem.read_u32(
                T.level_block(HINDENBIRD_LEVEL, self.save_file)
                + T.L_COMPLETE))
        except Exception:
            return False

    def is_boss(self, lid=None):
        lid = self.level_id() if lid is None else lid
        return lid in self.BOSS_LOSS or lid in self.HELMET_BOSSES

    def start_boss_loss(self):
        """Begin losing whichever boss fight Taz is in. Returns a deadline."""
        lid = self.level_id()
        if lid in self.HELMET_BOSSES:
            self._boss_anchor = self._pos()
            self._boss_bite = self.HELMET_BOSSES[lid]
            self._boss_helmet = True
            # The helmet route makes the game lose for real, so it raises a
            # Continue prompt. Claim that edge before boss_lost() sees it.
            self._forced_loss_until = time.time() + self.FORCED_LOSS_GRACE
            return time.time() + 12.0

        spec = self.BOSS_LOSS.get(lid)
        if not spec:
            return None
        self._boss_helmet = False
        self._boss_bite = False
        self._boss_hold = [f for f in spec["fields"] if f.get("hold")]
        park = spec.get("park")
        if park:
            self._write_pos(park)
        self._boss_anchor = park
        for f in spec["fields"]:
            try:
                if f.get("float"):
                    mem.write_bytes(f["addr"],
                                    struct.pack("<f", float(f["value"])))
                else:
                    mem.write_u32(f["addr"], int(f["value"]))
            except Exception:
                pass
        # Same claim as the helmet route: whatever this arena does about it,
        # the loss we just arranged must not be sent back out as ours.
        self._forced_loss_until = time.time() + self.FORCED_LOSS_GRACE
        return time.time() + spec["secs"]

    def boss_continue_answer(self):
        """1 = the player chose Continue, 2 = Quit, None = no prompt up.

        Read from the live object at [0x0037FC3C] + 0x0C: zeroed at
        0x001A1B84, set to 1 at 0x001A2438 and to 2 at 0x001A24F8.
        Nothing depends on this yet -- it is here because it is free, and
        because "they quit the fight" is a different event from "they lost
        it" if that ever matters.
        """
        if not mem:
            return None
        try:
            p = mem.read_u32(self.BOSS_CONTINUE_PTR)
            if not (self.BOSS_CONTINUE_LO <= p < self.BOSS_CONTINUE_HI):
                return None
            return mem.read_u32(p + self.BOSS_CONTINUE_ANSWER) or None
        except Exception:
            return None

    def hold_boss_loss(self):
        """Keep it applied until the fight resolves."""
        if getattr(self, "_boss_helmet", False):
            taz = mem.read_u32(T.TAZ_PTR) if mem else 0
            if mem and mem.valid_ptr(taz):
                c = mem.read_u32(taz + T.O_COSTUME_PTR)
                if mem.valid_ptr(c):
                    h = mem.read_u32(c + self.HELMET_CHAIN[0])
                    if mem.valid_ptr(h):
                        for off in (self.HELMET_CHAIN[1],
                                    self.HELMET_NEXT[1]):
                            try:
                                mem.write_u32(h + off, 0)
                            except Exception:
                                pass
            if self._boss_bite:
                ra = mem.deref(T.TAZ_PTR, T.O_BONUS_PTR, 0x10C)
                sa = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE)
                try:
                    if ra is not None and sa is not None and \
                            mem.read_u8(sa) == self.BITE_STATE:
                        mem.write_u32(ra, 0x00)
                except Exception:
                    pass
        else:
            for f in getattr(self, "_boss_hold", []):
                try:
                    mem.write_u32(f["addr"], int(f["value"]))
                except Exception:
                    pass

        anchor = getattr(self, "_boss_anchor", None)
        if anchor:
            cur = self._pos()
            if cur and any(abs(a - b) > 1.0 for a, b in zip(cur, anchor)):
                self._write_pos(anchor)

    # ------------------------------------------------------------ flow table

    # Disco Volcano leads straight into The Hindenbird. When the Hindenbird is
    # locked, the exit is pointed back at the hub instead -- the destination is
    # an ASCII name in a flow table, so it is rewritten in place.
    FLOW_HB_SLOT = 0x4B18B8
    FLOW_HUB = b"tazhub"
    FLOW_HB = b"tazboss2"

    def enforce_flow(self, hindenbird_granted):
        """Send the Disco Volcano exit to the hub while the Hindenbird is shut."""
        if not mem:
            return
        want = self.FLOW_HB if hindenbird_granted else self.FLOW_HUB
        try:
            cur = mem.read_bytes(self.FLOW_HB_SLOT, len(self.FLOW_HB))
        except Exception:
            return
        if cur.startswith(want):
            return
        # Padded to the original length: entries sit sixteen bytes apart and a
        # longer write destroys the next one.
        try:
            mem.write_bytes(self.FLOW_HB_SLOT,
                            want.ljust(len(self.FLOW_HB), b"\0"))
        except Exception:
            pass

    # -------------------------------------------------------------- effects

    # Filler and traps, as field writes. A powerup needs four things in the
    # costume object and, for the ones that change what a button does, a state
    # as well -- the flag alone does nothing, because the game still believes
    # no powerup is active.
    # `secs` is the value written to the duration field. `hold` is how long
    # the client keeps re-asserting it before clearing the flag, which is what
    # actually decides the length: writing the duration once is not enough,
    # because the game counts it down and something was cutting invisibility
    # to a couple of seconds. Holding it pins the length exactly.
    #
    # `sub` is +0x170 and is NOT universal -- see POWERUP_SUB. `material` is
    # the descriptor the game's own grant hands to 0x0023F3C8; without it Taz
    # is invisible to the enemies and opaque to the player.
    #
    # `reassert` False means the GAME owns the effect once it is granted, so
    # the hold must keep its hands off. Invisibility is the one: its tick runs
    # the timer, does the blink-out and tears the whole thing down by itself,
    # and every re-assertion the client made was fighting one of those. `hold`
    # for it is only a backstop -- deliberately longer than the effect, so the
    # game always finishes first and end_powerup finds nothing left to do.
    POWERUPS = {
        "pepper":       {"id": 4, "state": 0x3B, "flag": 0x1A0, "sub": 2,
                         "secs": 18.98, "hold": 19.0, "extra": {}},
        "invisibility": {"id": 2, "state": None, "flag": 0x194, "sub": None,
                         "material": MAT_INVISIBLE, "reassert": False,
                         "secs": INVIS_START, "hold": INVIS_SECONDS + 2.0,
                         "extra": {0x164: 0.0, 0x168: 0.75}},
        # Burp and hiccup are momentary: they are an action rather than a
        # state, so the flag is set and left to run its course.
        "burp":         {"id": None, "state": 0x00, "flag": 0x1D4,
                         "secs": None, "hold": 0.0, "extra": {},
                         "raw": {0x138: 0.1, 0x13C: 3}},
        "hiccup":       {"id": None, "state": 0x00, "flag": 0x198,
                         "secs": 1.82, "hold": 2.0, "extra": {0x164: 0.2},
                         "raw": {0x138: 0.03}},
    }

    # TWO FAMILIES, and the difference is HOW an effect enters a state.
    #
    #   _grant_powerup writes S_STATE DIRECTLY, into four fields. That skips
    #   whatever the state being left was going to do on the way out, and mid
    #   spin it breaks Taz's model -- observed, with burp. pepper and hiccup
    #   take the same path, so they carry the same hazard.
    #
    #   _install_state and _squash write S_REQUEST instead, after installing
    #   the handler -- which is exactly what the game's own 0x002C44D8 does.
    #   The game performs the transition itself, from whatever Taz is doing.
    #
    # That second group was put in the defer list by generalisation, not by
    # observation: "everything that writes a STATE belongs here". It does not.
    # Asking through the request field IS the game's mechanism, and the proof
    # is in the game -- spin into a stick of dynamite, an electric fence or a
    # squasher in vanilla and Taz stops and takes it. Deferring those was
    # inventing a restriction the engine does not have.
    #
    # It also made things worse than merely late. Holding a trap until the
    # spin ended, then cancelling the spin to hurry it along, fought the
    # player's own held button: cancel, SPINUP again, cancel, SPINUP again,
    # ten times a second until they let go of circle. Recorded with
    # taz_trap.py, which is why this is now two sets instead of one.
    DEFER_UNTIL_SAFE = {"burp", "pepper", "hiccup"}

    # Anything EXCEPT these. Waiting for one specific "safe" state meant
    # waiting for Taz to stand perfectly still, so burp never fired while
    # moving -- and before the state offset was corrected the test passed
    # constantly, which is why it looked fine.
    UNSAFE_TO_INTERRUPT = {
        0x0C, 0x0D, 0x0E,       # spinning
        0x2C, 0x2D, 0x3D, 0x3E, # drowning, falling, voiding out, crushed
        0x59, 0x5A,             # caught, losing a boss
        0x54, 0x55,             # the rest of the capture chain
        COASTER_STATE,          # on the rollercoaster
        *TRANSFORMED,           # a mouse, a ball, or changing between them
    }

    # The request-path traps. They may land on a spinning Taz -- that is the
    # point of them, and the game allows it -- but not on a dying or captured
    # one, where the game is already running a sequence of its own and the
    # trap would be wasted or would fight it.
    #
    # Derived by SUBTRACTION rather than written out again, so the two sets
    # cannot drift apart. Everything in this project that was two hand-written
    # copies of one fact has eventually disagreed with itself.
    REQUEST_PATH = {"dynamite", "electrocute", "bubblegum", "squash"}

    SAFE_HOLD = 0.15                    # seconds it must stay that way

    # Spin runs 0x0C start, 0x0D spin, 0x0E end.
    # Confirmed against the game: spinning reads 0x0C then 0x0D.
    SPIN_STATES = {0x0C, 0x0D, 0x0E}

    # See REQUEST_PATH. Spinning is deliberately not in here.
    NOT_PLAYABLE = UNSAFE_TO_INTERRUPT - SPIN_STATES

    # What the No Spinning trap actually cancels, which is NOT the same set.
    #
    # 0x0C SPINUP is where a spin should be stopped: nothing has happened yet,
    # so cancelling there reads as "he did not spin" rather than as a spin cut
    # short. Measured against both recordings at every phase offset, SPINUP
    # ALONE catches every spin -- it lasts 0.21s and 0.23s against a 0.1s
    # poll, so two samples always land inside it.
    #
    # 0x0D SPIN is kept anyway, as a backstop with a measured cost of nothing.
    # The margin on SPINUP is one poll: a spin whose start ran shorter than
    # 0.2s would slip through, and neither recording proves that cannot
    # happen. See taz_nospin_test.py, which prints the margin.
    #
    # 0x0E SPINDOWN is deliberately NOT here. That is the game ENDING a spin,
    # and cancelling it means fighting the recovery rather than the action --
    # which is also what made the old set dangerous, because 0x0C/0x0D/0x0E
    # are the cage-escape chain as well, and Taz: Haunted has two catchers
    # that cage him.
    NO_SPIN_STATES = {0x0C, 0x0D}

    TRAP_SECS = {"no_spin": 15.0, "squash": 10.0}
    BOUNTY_STEP = 5000

    # Recovering from a transformed death. Spinning is what turns Taz back,
    # and the request field is how to ask for it: no_spin already cancels a
    # spin that way, with the note that writing the state directly fights the
    # field the game drives every frame. This is the same lever pushed the
    # other way.
    SPIN_REQUEST = 0x0C
    UNBALL_FOR = 2.0

    # The gap between one effect ending and the next being allowed to start.
    #
    # hold_traps ends an effect and the queue starts the next one on the SAME
    # tick, because active_traps is empty again by then. A seed with two
    # thousand checks -- and Local Filler keeping most of its filler at home --
    # hands out enough of them to keep that queue permanently full, so effects
    # arrived one every tenth of a second. That is how the pepper got stuck:
    # the next grant wrote its state before the last teardown had settled.
    EFFECT_GAP = 1.5

    def force_spin(self):
        """Ask the game to put Taz into a spin. True if the request landed."""
        ra = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, S_REQUEST) if mem else None
        if ra is None:
            return False
        try:
            mem.write_u32(ra, self.SPIN_REQUEST)
            return True
        except Exception:
            return False

    def unball_tick(self):
        """Keep asking for the spin until Taz is out of the ball.

        One write lands on the frame the death happened, which the game may
        be far too busy to read, so it is re-asserted for a moment. Only
        while he is STILL the ball, the mouse, or nothing, though -- the
        instant he is anything else it stops, so a second spin can never be
        started once the first has taken.

        Returns a line to log the first time it writes, and None after.
        """
        until = getattr(self, "_unball_until", 0.0)
        if not until:
            return None
        if time.time() > until:
            self._unball_until = 0.0
            return None
        st = self.taz_state()
        if st is not None and st not in (0x00, BALL_STATE, MOUSE_STATE):
            self._unball_until = 0.0
            return None
        if not self.force_spin():
            return None
        if getattr(self, "_unball_said", False):
            return None
        self._unball_said = True
        return "died as the ball -- asked for a spin to turn Taz back"

    def taz_state(self):
        a = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE) if mem else None
        if a is None:
            return None
        try:
            return mem.read_u8(a)
        except Exception:
            return None

    def _settled(self, bad, attr):
        """Has Taz been out of `bad` for SAFE_HOLD?

        Held for a moment rather than sampled once: the client polls every
        tenth of a second and a single reading can land in a gap between two
        parts of an animation.
        """
        st = self.taz_state()
        now = time.time()
        if st is None or st in bad:
            setattr(self, attr, None)
            return False
        if getattr(self, attr, None) is None:
            setattr(self, attr, now)
            return False
        return now - getattr(self, attr) >= self.SAFE_HOLD

    def safe_to_interrupt(self):
        """Somewhere an effect that writes the state directly can start."""
        return self._settled(self.UNSAFE_TO_INTERRUPT, "_safe_since")

    def playable(self):
        """Somewhere a request-path trap can land. Spinning counts."""
        return self._settled(self.NOT_PLAYABLE, "_playable_since")

    def on_coaster(self):
        """Is Taz on a rollercoaster, or still being thrown off the end of one?

        Riding is a state of its own and is held for the whole ride, so the
        test is that state and nothing cleverer. The grace period is for the
        ending: the cart flings Taz clear and he picks himself up, and an item
        landing mid-flight breaks the sequence exactly as one landing mid-ride
        does. 3.17s of that was recorded, so 3.5s covers it.

        Deliberately not pinned to the two levels that have a ride. If a third
        one turns out to, this already handles it -- 0x4D is unambiguous, so
        there is no reason to narrow it the way the Taz: Haunted rule had to be.

        The grace deadline is kept fresh by death_tick, which runs every poll,
        NOT by this. Arming it here alone would not work: the client only calls
        grant_effect when something is waiting in the queue, so a ride with an
        empty queue would arm nothing, and an item arriving a moment after the
        cart ended would land mid-flight -- the exact case the grace exists for.
        """
        st = self.taz_state()
        if st == COASTER_STATE:
            self._coaster_until = time.time() + COASTER_GRACE
            return True
        return time.time() < self._coaster_until

    def effects_blocked(self):
        """Why an incoming filler or trap must not start right now, or None.

        One place, so a new reason is added once rather than in every branch
        of grant_effect.
        """
        if self.on_coaster():
            return "on a rollercoaster"
        if self.taz_state() in TRANSFORMED:
            return "a mouse or a ball"
        return None

    def _quiet_bounty(self):
        """Start holding our banner's slow-motion factor at 1.0."""
        now = time.time()
        self._bounty_quiet_until = now + T.BOUNTY_QUIET_FOR
        self._bounty_quiet_from = now + T.BOUNTY_QUIET_GRACE
        self._bounty_show_from = 0.0
        self._hold_bounty_factor()
        self._hide_bounty_logo()

    def _hold_bounty_factor(self):
        try:
            mem.write_bytes(T.BOUNTY_FACTOR, struct.pack("<f", 1.0))
        except Exception:
            pass

    def _hide_bounty_logo(self):
        """Clear the visible bit on the container holding the logo.

        The same bit Show sets at 0x0013BD24 and Hide clears at 0x0013BF50.
        The driver's own liveness gate reads the container POINTER, not this
        flag, so hiding it changes nothing else -- and state 4's teardown Hide
        finds the bit already clear and leaves it exactly where it was going
        to put it.
        """
        try:
            box = mem.read_u32(T.BOUNTY_LOGO_BOX)
            if not mem.valid_ptr(box):
                return
            a = box + T.BOX_FLAGS
            mem.write_u32(a, mem.read_u32(a) & ~T.BOX_VISIBLE)
        except Exception:
            pass

    def _bounty_number_only(self):
        """Put the banner straight on the number, skipping the caption.

        State 2 is a NextPage (0x0020232C), so choosing the cash page and then
        letting state 2 run would wrap it back to the caption. Skipping
        straight to the counting state is what makes the choice stick.

        If the trampoline is not there the page cannot be set, and that is
        fine: the banner then runs its normal course -- caption, then number
        -- just without the logo. Worse, not broken.
        """
        if N is None:
            return
        try:
            box = mem.read_u32(T.BOUNTY_TEXT_BOX)
            if not mem.valid_ptr(box):
                return
            if N.call(T.BOX_SET_PAGE, box, T.BOUNTY_CASH_PAGE) is None:
                return
            mem.write_u32(T.POPUP_STATE_ADDR, T.BOUNTY_COUNTING)
        except Exception:
            pass

    def bounty_tick(self):
        """Keep our own bounty banner running at normal speed.

        One read a tick while a banner of ours is up, nothing at all
        otherwise. Stops as soon as the banner tears down -- but not during
        the first moment, because the driver has not set the state word yet
        and a zero there would look like the banner had already finished.
        """
        if not getattr(self, "_bounty_quiet_until", 0.0):
            return
        now = time.time()
        if now > self._bounty_quiet_until:
            self._bounty_quiet_until = 0.0
            return
        try:
            state = mem.read_u32(T.POPUP_STATE_ADDR)
            if now > self._bounty_quiet_from and not state:
                self._bounty_quiet_until = 0.0
                self._bounty_show_from = 0.0
                return
            if state == T.BOUNTY_SHOWING:
                # The count-up. Hold it, so the number the player was sent
                # is still on screen by the time they look at it.
                if not self._bounty_show_from:
                    self._bounty_show_from = now
                if now - self._bounty_show_from < T.BOUNTY_HOLD:
                    mem.write_bytes(
                        T.BOUNTY_EXPIRY,
                        struct.pack("<f", mem.read_float(T.GAME_TIME) + 0.5))
        except Exception:
            self._bounty_quiet_until = 0.0
            return
        self._hold_bounty_factor()
        # Insurance. Nothing re-shows the logo on the path we take -- only
        # state 1's handler does, and we never enter state 1 -- but this is
        # two words beside two we are already writing.
        self._hide_bounty_logo()

    def bounty_ready(self):
        """Whether the game's own bounty banner can be raised right now.

        0x00201DD0 has no guards of its own -- it never reads GAME_STATE at
        all -- so everything it assumes has to be true before we call it. Each
        test below is one of those assumptions, and each names the instruction
        that would suffer:

          * BOUNTY_WIDGET is built. It is dispatched through a vtable at
            0x00202068 and 0x00202088 with no null check, so a null there
            reads +0x114/+0x134 out of low memory and jalrs to whatever it
            finds. This is the game's OWN gate: its driver tests exactly this
            word at 0x0020214C before doing anything (0x00202170).

          * No banner is already up. One IS survivable -- 0x00201E08 flushes
            the in-flight total into the save first, so no money is lost --
            but ours would cut the game's own message off mid-sentence, which
            is the thing this whole feature exists to avoid.

          * A real save slot. 0x00201E90 reads it as a SIGNED BYTE and
            multiplies by the file stride without testing for the -1 "no file
            loaded" sentinel, so calling this on the title screen writes a
            whole file stride BELOW the save area.

          * A level id the save record can hold. lid * 0x238 past the record's
            0x42B4 walks into the next slot's data.

          * The player is actually playing, and can see it. GAME_STATE 1, and
            not during one of the game's slowdowns -- which is also where the
            banner's own text lives, so raising ours there would land two
            banners on each other.

        Anything false means "not yet", never "not at all": grant_effect
        returns "defer" and the client asks again on the next tick.
        """
        if mem is None:
            return False
        try:
            if not mem.valid_ptr(mem.read_u32(T.BOUNTY_WIDGET)):
                return False
            if mem.read_u32(T.POPUP_STATE_ADDR):
                return False
            slot = mem.read_u8(T.CURRENT_FILE)
            if slot > 0x7F or slot > 3:          # signed byte; -1 = no file
                return False
            lid = mem.read_u8(T.CURRENT_LEVEL_BYTE)
            if not D.FIRST_LEVEL_ID <= lid <= 29:
                return False
            if self.game_state() != T.STATE_ACTIVE:
                return False
        except Exception:
            return False
        # The slowdown gate notify already owns. Its whole job is "does the
        # game have its own words on screen", which is precisely the question.
        return not (N is not None and N.slowed())

    def grant_effect(self, name):
        """Apply one filler item or trap.

        Returns a deadline for anything that has to be held, None for a single
        write, and the string "defer" when it must not start yet.
        """
        # Some places take everything, not just the effects that are awkward
        # to start: a scripted rollercoaster, and Taz: Haunted's mouse and
        # ball. A trap, a filler and a powerup all break those equally, which
        # is why this sits ahead of the per-effect DEFER_UNTIL_SAFE test
        # rather than inside it.
        #
        # The queue holds rather than drops: client.py leaves a deferred
        # effect at the head and tries again next tick, so a long ride or a
        # long stint as the ball means they arrive afterwards, not never.
        if self.effects_blocked():
            return "defer"
        # Writes S_STATE directly, so it must not start mid-spin.
        if name in self.DEFER_UNTIL_SAFE and not self.safe_to_interrupt():
            return "defer"
        # Asks through S_REQUEST, so a spin is fine -- but a death or a
        # capture is not.
        if name in self.REQUEST_PATH and not self.playable():
            return "defer"
        if name == "bounty":
            # Have the GAME award it. 0x00201DD0 credits the level's bounty
            # and the running total and plays the banner, all three, and it
            # works out the save slot itself -- which is the half this used to
            # get wrong. Deferred rather than dropped when the moment is not
            # right; the client leaves a deferred effect at the head of the
            # queue and asks again next tick.
            lid = self.level_id()
            if lid not in D.LEVEL_ORDER:
                return "defer"
            if not self.bounty_ready():
                return "defer"
            if N is not None and N.call(T.BOUNTY_POPUP, T.BOUNTY_STRING,
                                        self.BOUNTY_STEP) is not None:
                # Immediately, not on the next poll: the driver runs once a
                # frame and would otherwise get one look at 0.25, and one
                # frame of the logo.
                self._quiet_bounty()
                self._bounty_number_only()
                return None
            # No trampoline, so no banner. Write what the game would have
            # written, at the addresses it would have written them, so the
            # money is at least real and the pause total agrees.
            try:
                a = D.level_block(lid, self.save_file) + D.L_TOTAL_BOUNTY
                mem.write_u32(a, mem.read_u32(a) + self.BOUNTY_STEP)
                b = T.TOTAL_BOUNTY_SAVE + self.save_file * D.FILE_STRIDE
                mem.write_u32(b, mem.read_u32(b) + self.BOUNTY_STEP)
            except Exception:
                pass
            return None
        if name == "lose_costume":
            # Taking the costume off is exactly what beating a keeper looks
            # like from the outside, so the judge has to be told -- otherwise
            # the trap confirms a takedown that never happened.
            #
            # But ONLY when something actually comes off. Telling it
            # unconditionally meant a trap landing on an undressed Taz still
            # vetoed two seconds of genuine takedowns, for a write that did
            # nothing. Told before the write, not after, because the client
            # polls and the costume must never be seen gone un-explained.
            a = mem.deref(T.TAZ_PTR, T.O_COSTUME_PTR, T.C_COSTUME)
            try:
                if a is not None and mem.read_u8(a) != T.COSTUME_NONE:
                    if getattr(self, "_catchers", None) is not None:
                        self._catchers.note_costume_strip()
                    mem.write_u8(a, T.COSTUME_NONE)
            except Exception:
                pass
            return None
        if name == "no_spin":
            return time.time() + self.TRAP_SECS[name]
        if name == "dynamite":
            return self._install_state(T.EAT_BAD_FOOD_FN,
                                       T.EAT_BAD_FOOD_STATE)
        if name == "electrocute":
            return self._install_state(T.ELECTROCUTE_FN,
                                       T.ELECTROCUTE_STATE)
        if name == "bubblegum":
            return self._install_state(T.BUBBLEGUM_FN, T.BUBBLEGUM_STATE)
        if name == "squash":
            return self._squash()
        return self._grant_powerup(name)

    def _squash_bit(self, on):
        """Set or clear the bit UNSQUASHTAZ toggles. Returns True if done."""
        actor = mem.read_u32(T.ACTOR_PTR) if mem else 0
        if not mem or not mem.valid_ptr(actor):
            return False
        a = actor + T.O_ACTOR_FLAGS
        try:
            v = mem.read_u32(a)
            mem.write_u32(a, (v | T.SQUASH_BIT) if on
                          else (v & ~T.SQUASH_BIT))
            return True
        except Exception:
            return False

    def _squash(self):
        """Flatten Taz, and hold him flat until hold_traps lets him up."""
        if not self._squash_bit(False):
            return None
        taz = mem.read_u32(T.TAZ_PTR) if mem else 0
        if not mem or not mem.valid_ptr(taz):
            self._squash_bit(True)
            return None
        obj = mem.read_u32(taz + T.O_STATE_PTR)
        if not mem.valid_ptr(obj):
            self._squash_bit(True)
            return None
        try:
            # No handler: STATE_MOVESQUASHED is built in, unlike the
            # dynamite and the shock.
            mem.write_u32(obj + T.S_HANDLER, 0)
            mem.write_u32(obj + T.S_REQUEST, T.SQUASH_STATE)
        except Exception:
            self._squash_bit(True)
            return None
        return time.time() + self.TRAP_SECS["squash"]

    def _install_state(self, handler, state):
        """Install a state handler and ask for its state.

        Exactly what 0x002C44D8 does, in the same order: the handler first,
        because the game reads it when the state is entered, and then the
        request rather than the state itself -- the state field is driven
        every frame and writing it directly loses the argument.

        The game clears both within about a second, which is the trap
        having run, so there is nothing to hold or to end.
        """
        taz = mem.read_u32(T.TAZ_PTR) if mem else 0
        if not mem or not mem.valid_ptr(taz):
            return None
        obj = mem.read_u32(taz + T.O_STATE_PTR)
        if not mem.valid_ptr(obj):
            return None
        try:
            mem.write_u32(obj + T.S_HANDLER, handler)
            mem.write_u32(obj + T.S_REQUEST, state)
        except Exception:
            return None
        return None

    def _grant_powerup(self, name):
        spec = self.POWERUPS.get(name)
        if not spec:
            return None
        self._powerup_active = name
        taz = mem.read_u32(T.TAZ_PTR) if mem else 0
        if not mem or not mem.valid_ptr(taz):
            return None
        c = mem.read_u32(taz + T.O_COSTUME_PTR)
        if not mem.valid_ptr(c):
            return None
        try:
            if spec["secs"]:
                mem.write_bytes(c + T.C_POWER_TIME,
                                struct.pack("<f", spec["secs"]))
            for off, val in spec.get("extra", {}).items():
                mem.write_bytes(c + off, struct.pack("<f", val))
            for off, val in spec.get("raw", {}).items():
                if isinstance(val, float):
                    mem.write_bytes(c + off, struct.pack("<f", val))
                else:
                    mem.write_u32(c + off, val)
            if spec["id"] is not None:
                mem.write_u32(c + T.C_ACTIVE_ID, spec["id"])
            # Only where the game's own grant writes it. Invisibility's does
            # not, and 2 over a packed word was three bytes of collateral.
            sub = spec.get("sub", T.POWERUP_SUB.get(name))
            if sub is not None:
                mem.write_u32(c + T.C_ACTIVE_SUB, sub)
            mem.write_u8(c + spec["flag"], 1)
        except Exception:
            return None

        # The visual, for the ones that have one. Setting the flag alone left
        # Taz solid until a spin re-applied the material for us -- see the
        # O_MAT_MODE comment for the four words and where they came from.
        self._material_wrote = []
        if spec.get("material") is not None:
            self._write_material(taz, spec["material"])

        # Only an effect that changes what a button does needs a state;
        # invisibility is passive and leaves it alone.
        #
        # Every one of these is photographed before it is overwritten, because
        # end_powerup used to clear the flag, the id and the timer and leave
        # the STATE exactly where it was put. For the chili pepper that state
        # is what square does, so square stayed fire -- for the rest of the
        # run, through deaths and level changes, with nothing left flagged to
        # explain why.
        st = spec.get("state")
        self._powerup_wrote = []
        if st is not None:
            for off, s_off, e_off in ((T.O_STATE_PTR, T.S_STATE, 0x204),
                                      (T.O_BONUS_PTR, 0x0B0, 0x0B4)):
                obj = mem.read_u32(taz + off)
                if not mem.valid_ptr(obj):
                    continue
                for field in (s_off, e_off):
                    a = obj + field
                    try:
                        was = mem.read_u32(a)
                        mem.write_u32(a, st)
                        self._powerup_wrote.append((a, was, st))
                    except Exception:
                        pass
        hold = spec.get("hold", 0.0)
        return (time.time() + hold) if hold else None

    def _material_pairs(self, taz, param):
        """The words 0x0023F3C8(taz, param) writes, in its own order.

        Slot 0 unconditionally; slot 1 only when bit 1 of MAT_FLAGS is clear,
        which is the branch at 0x0023F404.
        """
        pairs = [(taz + T.O_MAT_MODE, 3), (taz + T.O_MAT_PARAM, 0)]
        try:
            flags = mem.read_u32(T.MAT_FLAGS)
        except Exception:
            flags = 0
        if not flags & T.MAT_SKIP_SLOT1:
            pairs += [(taz + T.O_MAT_MODE + 4, 4),
                      (taz + T.O_MAT_PARAM + 4, param)]
        return pairs

    def _write_material(self, taz, param):
        """Apply it, photographing what was there so it can be put back.

        Photographed rather than reconstructed on the way out: the game's own
        restore (0x0023F1A8) branches on two globals whose writers were never
        found, so reproducing it would be a guess where remembering is a fact.
        """
        self._material_wrote = []
        for a, val in self._material_pairs(taz, param):
            try:
                was = mem.read_u32(a)
                mem.write_u32(a, val)
                self._material_wrote.append((a, was, val))
            except Exception:
                pass

    # There is deliberately no _reassert_material. It existed, and it was
    # wrong: the blink IS the game removing the material and putting it back,
    # so anything that noticed the gaps and filled them in was cancelling the
    # blink. Nothing needs to hold this -- every path that takes the material
    # off without the game meaning it (a spin, a model swap, electrocution)
    # re-reads C_INVISIBLE and re-applies on its own, and death runs
    # 0x001C68E8, which is a correct full teardown.

    def _restore_material(self):
        """Put Taz's material back where the grant found it.

        Compare-and-restore, for exactly the reason _restore_powerup_state is:
        the game re-applies this itself on a spin, a model swap and three
        other paths, so if the field no longer holds what we wrote then the
        game's value is the current one and ours is stale.
        """
        for a, was, wrote in getattr(self, "_material_wrote", ()):
            try:
                if mem.read_u32(a) == wrote:
                    mem.write_u32(a, was)
            except Exception:
                pass
        self._material_wrote = []

    def _restore_powerup_state(self):
        """Undo the state fields _grant_powerup overwrote.

        Compare-and-restore: put the old value back ONLY where the field still
        holds exactly what we wrote. If the game has moved Taz on since, its
        value is the correct one and ours is stale -- writing over that would
        be the same mistake in the other direction.

        Which also means this does nothing in the ordinary case, because the
        game drives the state every frame. It only bites when the field is
        genuinely stuck, which is the case that was breaking square.
        """
        for a, was, wrote in getattr(self, "_powerup_wrote", ()):
            try:
                if mem.read_u32(a) == wrote:
                    mem.write_u32(a, was)
            except Exception:
                pass
        self._powerup_wrote = []

    def end_squash(self):
        """Put bit 0x40 back, which is all UNSQUASHTAZ does."""
        self._squash_bit(True)

    def end_powerup(self, name):
        """Clear a powerup once its time is up."""
        spec = self.POWERUPS.get(name)
        taz = mem.read_u32(T.TAZ_PTR) if mem else 0
        if not mem:
            return
        # Before anything that can bail out. The costume pointer going bad is
        # exactly the moment a stuck state would otherwise be left behind --
        # and a stuck material would leave Taz half-transparent for the rest
        # of the run, which is the most visible way this can fail.
        self._restore_powerup_state()
        self._restore_material()
        if not spec or not mem.valid_ptr(taz):
            return
        c = mem.read_u32(taz + T.O_COSTUME_PTR)
        if not mem.valid_ptr(c):
            return
        try:
            mem.write_u8(c + spec["flag"], 0)
            if spec["id"] is not None:
                mem.write_u32(c + T.C_ACTIVE_ID, T.C_ACTIVE_NONE)
            mem.write_bytes(c + T.C_POWER_TIME, b"\0\0\0\0")
        except Exception:
            pass

    def hold_traps(self, active):
        """Keep the timed effects applied. `active` maps name -> deadline."""
        now = time.time()
        done = []
        for name, until in list(active.items()):
            if now >= until:
                done.append(name)
                continue
            if name in self.POWERUPS:
                # Re-assert the duration so the game cannot cut it short.
                #
                # Except where the game is the one running it. Invisibility's
                # tick counts its own timer up, blinks Taz out over the last
                # five seconds by REMOVING and re-adding the material, and
                # ends the effect itself. Re-asserting the timer parks it
                # below the blink threshold and re-asserting the material
                # fills in every gap the blink makes, so between them the
                # client was holding the effect open and holding it solid.
                spec = self.POWERUPS[name]
                if not spec.get("reassert", True):
                    continue
                if spec.get("secs"):
                    taz = mem.read_u32(T.TAZ_PTR) if mem else 0
                    if mem and mem.valid_ptr(taz):
                        c = mem.read_u32(taz + T.O_COSTUME_PTR)
                        if mem.valid_ptr(c):
                            try:
                                mem.write_bytes(
                                    c + T.C_POWER_TIME,
                                    struct.pack("<f", spec["secs"]))
                                mem.write_u8(c + spec["flag"], 1)
                            except Exception:
                                pass
                continue
            if name == "squash":
                continue          # nothing to hold; the bit does the work
            if name == "no_spin":
                # Detect on the real state field, act on the request field --
                # they live in the same object, 0x10C apart. The detection was
                # reading the old offset, so this trap was firing on whatever
                # a pointer's low byte happened to be.
                #
                # Writing the state directly would fight the game, which
                # drives that field every frame; the request is the input it
                # reads, so cancelling there is the game's own mechanism.
                sa = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE)
                ra = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, S_REQUEST)
                try:
                    if ra is not None and sa is not None:
                        if mem.read_u8(sa) in self.NO_SPIN_STATES:
                            mem.write_u32(ra, IDLE_STATE)
                except Exception:
                    pass
        return done

    # ------------------------------------------------------------- catchers

    def start_catchers(self, known):
        """Prepare catcher tracking from the recorded positions.

        `known` is the taz_catchers.json structure. All the judging lives in
        CatcherJudge, which never touches memory -- this is only the wiring.
        """
        table, radii = {}, {}
        for lid, rec in (known or {}).items():
            table[int(lid)] = [c["pos"] for c in rec.get("catchers", [])]
            # Recorded per level and previously dropped here, so nothing ever
            # read it. It does not gate anything -- it only lets the judge say
            # in its trace when a keeper turned up nowhere near a known post.
            if rec.get("radius"):
                radii[int(lid)] = rec["radius"]
        self._catchers = CatcherJudge(posts=table, level_radius=radii)
        self._catcher_posts = table
        self._catcher_level = None
        self._despawned = set()
        self.catcher_why = []
        self.catcher_lost = []      # a takedown seen but impossible to name
        self.catcher_blind = []     # things the player needs told out loud
        self.despawn_seen = []      # what would have been sent away
        return sum(len(v) for v in table.values())

    def uncredit_catcher(self, lid, idx):
        """The client refused to send this one; let it be earned again."""
        if getattr(self, "_catchers", None) is not None:
            self._catchers.uncredit(lid, idx)

    def catcher_tick(self, credited=()):
        """Newly defeated catchers, as (level, index) pairs.

        Reading only. What any of it means is CatcherJudge's business, so the
        rules can be tested offline -- see taz_catcher_test.py -- rather than
        only by playing the game and hoping.

        `credited` is the client's set of (level, index) already sent. Keepers
        matching one of those are despawned, so a player can see at a glance
        which catchers in a level they still owe.
        """
        if not getattr(self, "_catchers", None):
            return []
        lid = self.level_id()
        if lid is None:
            return []
        # PER TICK. This list was only ever cleared in __init__ and
        # start_catchers, so anything appended to it stayed for the whole
        # session -- and client.py reports the FIRST entry each time, rate
        # limited to one a minute. One keeper in Yosemite Zoo therefore
        # produced the same sentence every sixty seconds for the rest of the
        # run, in every level, long after leaving the one it was about.
        self.catcher_blind = []
        # NOT DURING A LOAD. This ran unconditionally, and a level transition
        # is exactly the moment LEVEL_ID and the enemy lists disagree: the
        # incoming level's keepers are built while the id still reads the one
        # being left. That is how five Tazland keepers came to be judged
        # against Yosemite Zoo's single post and reported as "the positions
        # in taz_catchers.json are wrong for this level".
        #
        # Nothing true can be concluded from a half-loaded level, so nothing
        # is. A takedown cannot be missed this way -- the player is not
        # fighting anything during a loading screen.
        if self.game_state() != STATE_ACTIVE:
            return []
        if lid != self._catcher_level:
            self._catcher_level = lid
            self._catchers.enter_level(lid)
            self._despawned = set()

        try:
            ps2 = T.TazPS2(mem)
            keepers = ps2.catchers()
            # Whether both list walks agreed with their own count fields. A
            # short walk is a torn read, not a departure, and the judge must
            # not book takedowns from one.
            walk_ok = ps2.walk_ok
        except Exception as exc:
            # Returning [] here is right -- a failed read must not conclude
            # anything. But doing it in silence means the judge simply stops
            # watching, for as long as the reads keep failing, and a takedown
            # inside that stretch leaves no trace anywhere. Say it happened.
            self.catcher_blind.append(
                (lid, "could not read the keepers (%s) -- the judge is not "
                      "watching" % exc.__class__.__name__))
            return []

        costume = None
        state = None
        try:
            a = mem.deref(T.TAZ_PTR, T.O_COSTUME_PTR, T.C_COSTUME)
            if a is not None:
                costume = mem.read_u8(a)
            b = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE)
            if b is not None:
                state = mem.read_u8(b)
        except Exception:
            pass

        killed = self._catchers.poll(lid, keepers, costume, state,
                                     self._read_enemy_total(),
                                     complete=walk_ok)
        self.catcher_why = list(self._catchers.why)
        self.catcher_lost = [(lid, i, why) for i, why in self._catchers.lost]
        # Whatever catchers() already appended this tick STAYS -- it is the
        # record of the judge not having been able to look at all.
        self.catcher_blind += [(lid, w) for w in self._catchers.blind]

        # Anything already banked is sent away. After the poll, so the judge
        # has this tick's sighting recorded before the write lands.
        self._despawn(lid, credited, keepers)

        return [(lid, i) for i in killed]

    def _despawn(self, lid, credited, keepers):
        """Send away keepers whose check the player already has.

        They respawn on every visit, so without this a level with four
        catchers looks the same on the fourth visit as on the first and the
        player has to remember which ones they still owe.

        Once per pointer. Keepers stream in as Taz gets near, and each one
        arrives as a NEW pointer, so this keeps working through a level rather
        than firing once on the way in -- and a fresh visit re-spawns them all,
        which is why `_despawned` is cleared on a level change.

        `despawn_seen` records what was sent away, for the log.
        """
        if not credited:
            return
        # Never while a takedown is in flight. A despawn books a count drop
        # of its own, and a booking in the air at the wrong moment is what
        # used to swallow the real one. Waiting costs nothing -- the keeper
        # is still there a second later, and it is not going anywhere.
        if getattr(self._catchers, "pending", None):
            return
        try:
            targets = self._catchers.despawn_targets(lid, credited, keepers)
        except Exception:
            return
        for ptr, idx in targets:
            if ptr in self._despawned:
                continue
            self._despawned.add(ptr)
            self.despawn_seen.append((lid, idx))
            if not DESPAWN_RECIPE:
                continue
            try:
                sub = mem.read_u32(ptr + E_SUB)
                if not mem.valid_ptr(sub):
                    continue
                base = {"obj": ptr, "sub": sub}
                for where, off, op, value in DESPAWN_RECIPE:
                    a = base[where] + off
                    mem.write_u32(a, (mem.read_u32(a) | value)
                                  if op == "|" else value)
                self._catchers.note_despawn(ptr)
            except Exception:
                pass

    def _match_post(self, lid, pos):
        """Which recorded post a keeper is nearest.

        The judge owns this now; kept as a name because the recorder scripts
        call it.
        """
        if not getattr(self, "_catchers", None):
            return None
        return self._catchers.match_post(lid, pos)

    def _read_enemy_total(self):
        try:
            v = mem.read_u32(TOTAL_ENEMY_COUNT)
        except Exception:
            return None
        return v if 0 <= v < 1000 else None

    def catcher_debug(self):
        """Everything the kill detection looks at, for one tick.

        Reports the judge's own reasoning alongside the raw readings, so a
        recording says WHY a takedown was or was not credited rather than
        leaving that to be reconstructed from the numbers.
        """
        lid = self.level_id()
        out = {"level": lid, "enemy_total": self._read_enemy_total()}
        try:
            keepers = T.TazPS2(mem).catchers()
        except Exception:
            keepers = []
        out["loaded"] = len(keepers)
        out["defeated"] = sum(1 for k in keepers if k.get("defeated"))
        out["keepers"] = [
            {"ptr": k["ptr"], "defeated": bool(k.get("defeated")),
             "anim": k.get("anim"), "pos": k.get("pos")}
            for k in keepers]
        try:
            a = mem.deref(T.TAZ_PTR, T.O_COSTUME_PTR, T.C_COSTUME)
            out["costume"] = mem.read_u8(a) if a is not None else None
            b = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE)
            out["state"] = mem.read_u8(b) if b is not None else None
        except Exception:
            pass
        j = getattr(self, "_catchers", None)
        if j is not None:
            out["pending"] = [dict(p) for p in j.pending]
            out["drops"] = len(j.drops)
            out["why"] = list(j.why)
            out["despawned"] = len(getattr(self, "_despawned", ()))
        return out

    # ------------------------------------------------------------ geofences

    def load_gates(self, path=None):
        """Read the recorded pushback zones.

        Some doors cannot be locked through the access field: Zooney Tunes has
        to stay open for hub 1's other two doors to exist at all, Tazland is
        natively open, and the final boss doors sit at its entrance. Those are
        guarded by position instead -- walk too close without the unlock and
        you are pushed back out.

        Read through _imports.data so it works from a .apworld zip as well as
        a folder. Building a path from __file__ points inside the archive when
        zipped, which is why this reported "not found" while the file was
        plainly there.
        """
        raw = _imports.data("taz_gates.json")
        if not raw:
            self.gates = []
            return False

        # Two shapes in the file: hub doors, keyed by the level they lead to
        # and carrying one trigger point, and named zones with several points.
        # Both are flattened so enforcement has one thing to walk.
        self.gates = []
        for key, g in raw.items():
            points = g.get("points")
            if points is None and "trigger" in g:
                points = [g["trigger"]]
            if not points:
                continue
            dest = g.get("gates")
            if dest is None:
                try:
                    dest = int(key)
                except (TypeError, ValueError):
                    continue
            self.gates.append({
                "name": g.get("name", key),
                "where": g.get("hub", g.get("in")),
                "gates": int(dest),
                "radius": float(g.get("radius", 800.0)),
                "points": [tuple(float(v) for v in p) for p in points],
            })
        return True

    # Tazland used to eject to the level's start, because a plain shove out
    # of one zone could land the player inside the next. The shock-and-drift
    # pushback holds there on its own -- tested with every gate locked -- so
    # these are ordinary doors now. Kept because the grouping is still true.
    EJECT_ZONES = {"tazland-bridge", "disco-volcano", "hindenbird"}

    def load_exits(self):
        """Where each level ends, recorded with taz_exit_recorder.py."""
        raw = _imports.data("taz_exits.json") or {}
        self.exits = {int(k): v for k, v in raw.items()}
        return bool(self.exits)

    def at_level_exit(self, lid):
        """Is Taz standing where this level ends?

        Without a recording the answer is yes, so a missing file degrades to
        the old poster-only behaviour rather than making completion
        unreachable -- a check that cannot fire is worse than one that fires
        early.
        """
        rec = getattr(self, "exits", None) or {}
        spot = rec.get(lid)
        if not spot:
            return True
        pos = self._pos()
        if not pos:
            return False
        return _dist(pos, spot["pos"]) <= float(spot.get("radius", 700.0))

    def posters_done(self, lid):
        """Are all seven of a level's posters destroyed?"""
        base = T.level_block(lid, self.save_file)
        try:
            return all(mem.read_u32(base + T.L_POSTER + i * 4)
                       for i in range(T.POSTERS_PER_LEVEL))
        except Exception:
            return False

    # Blocking a door: shock Taz, then drift him back out while he is stunned.
    # A shove alone read as the game glitching, and left him standing in the
    # trigger to be shoved again.
    GATE_DRIFT = 1.4          # seconds the drift takes
    GATE_MARGIN = 1.45        # multiple of the radius he ends up at
    GATE_EXTRA = 200.0        # flat distance on top of that
    GATE_MIN_GAP = 0.4        # floor between two shocks from one gate

    def enforce_gates(self, granted_levels, granted_bosses, spawns=None):
        self.touched_disco_volcano = False
        """Refuse a locked door by electrocuting Taz and easing him back out.

        A gate re-arms by Taz LEAVING it, never by a timer. A wall-clock
        cooldown let him sprint most of the way back in before the gate could
        fire again -- 760 units in three seconds -- and he got through. Now
        leaning on a door means being caught at the boundary every time.
        """
        if not getattr(self, "gates", None):
            return []
        lid = self.level_id()
        pos = self._pos()
        if pos is None or lid is None:
            return []
        now = time.time()

        glide = getattr(self, "_gate_glide", None)
        if glide is not None:
            if glide["level"] != lid:
                self._gate_glide = None          # a load ended it
                return []
            f = (now - glide["t0"]) / self.GATE_DRIFT
            if f >= 1.0:
                self._write_pos(glide["to"])
                self._gate_glide = None
            else:
                # Ease out: away quickly, settling gently.
                e = 1.0 - (1.0 - f) ** 2
                a, b = glide["from"], glide["to"]
                self._write_pos(tuple(a[i] + (b[i] - a[i]) * e
                                      for i in range(3)))
            return []

        armed = self.__dict__.setdefault("_gate_armed", {})
        last = self.__dict__.setdefault("_gate_last", {})

        for gate in self.gates:
            if gate["where"] not in (None, lid):
                continue
            name = gate["name"]
            dest = gate["gates"]
            if dest in granted_levels or dest in granted_bosses:
                armed[name] = True
                continue

            radius = gate["radius"]
            hit = None
            for point in gate["points"]:
                if _dist(pos, point) < radius:
                    hit = point
                    break
            if hit is None:
                armed[name] = True               # left the zone: armed again
                continue

            # Tazland is only marked complete by ENTERING Disco Volcano, which
            # Open mode blocks -- so the check would be unreachable. Walking
            # into that door with every poster destroyed is the same act, so
            # it counts: the player is turned back, the level is finished.
            if name == "disco-volcano":
                self.touched_disco_volcano = True

            if not armed.get(name, True):
                continue
            if now - last.get(name, 0.0) < self.GATE_MIN_GAP:
                continue
            armed[name] = False
            last[name] = now
            self._install_state(T.ELECTROCUTE_FN, T.ELECTROCUTE_STATE)

            want = radius * self.GATE_MARGIN + self.GATE_EXTRA
            self._gate_glide = {"from": pos, "to": _gate_exit(pos, hit, want),
                                "t0": now, "level": lid}
            break
        # Silent: this fires for as long as the player leans on the door,
        # which would flood the log.
        return []

    def _write_pos(self, pos):
        a = mem.deref(T.TAZ_PTR, 0xC0) if mem else None
        if a is None:
            return False
        try:
            mem.write_floats(a, tuple(float(v) for v in pos))
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- deaths

    def _pos(self):
        a = mem.deref(T.TAZ_PTR, 0xC0) if mem else None
        if a is None:
            return None
        try:
            p = mem.read_floats(a, 3)
        except Exception:
            return None
        if not p or any(abs(v) > 1e7 for v in p):
            return None
        return p

    def _state(self):
        a = mem.deref(T.TAZ_PTR, T.O_STATE_PTR, T.S_STATE) if mem else None
        if a is None:
            return None
        try:
            return mem.read_u8(a)
        except Exception:
            return None

    def death_tick(self):
        """One death, reported once, from the game's own state.

        Every death Taz can suffer has a state of its own:

            0x59  caught by a keeper
            0x2C  drowned
            0x2D  fell out of the world
            0x3E  a third way out

        Position jumps were used for the last of those, because the falling
        state had not been found -- and that inference could not tell a fall
        from an in-level warp, so warping reported a death. It is gone.

        A state is also immune to everything that made the jump unreliable:
        our own teleports, respawns, and hub doors all leave it alone.
        """
        lid = self.level_id()
        if lid is None or lid not in T.LEVEL_IDS:
            self._last_state = None
            return None

        # A load leaves the state object half-built, so a reading taken with
        # no known-good previous state is meaningless.
        #
        # But the death itself comes FIRST. A crush or a drown reloads almost
        # at once, so the death state and the loading screen arrive together --
        # and suppressing everything during the load threw the death away with
        # the noise. Only a fall, which has a longer animation, got through.
        #
        # So the transition is judged on whether there is a trustworthy
        # previous state, not on what the loading screen is doing. The window
        # only stops a reading being trusted when there is nothing to compare
        # it against.
        st = self._state()
        obj = mem.follow(T.TAZ_PTR, T.O_STATE_PTR) if mem else None
        prev = self._last_state
        loading = self.game_state() != STATE_ACTIVE

        if lid != self._death_level:
            self._death_level = lid
            self._last_state = None
            self._coaster_left = 0.0
            self._transform_left = 0.0
            self._last_state_obj = None
            return None

        # Nothing counts until Taz is demonstrably ALIVE -- a normal state,
        # not a death and not the zero a half-built object reads. A fixed
        # five-second window was both too long and too short: it swallowed
        # real deaths inside a boss phase, and still let a cutscene's CRUSHED
        # through if the cutscene ran long. Waiting for him to actually be
        # doing something is the condition that was meant all along.
        if prev is None:
            if not loading and st is not None and st not in DEATH_STATES \
                    and st != 0x00:
                self._last_state = st
            return None

        self._last_state = st
        was_obj, self._last_state_obj = self._last_state_obj, obj

        # ---------------------------------------------------- rollercoaster
        #
        # This has to run BEFORE the "nothing changed" return below, because
        # the verdict is reached by time passing rather than by a transition:
        # after a coaster death Taz sits in MOVE, and sitting in MOVE is not
        # an event. Everything else in this function is edge-triggered; this
        # one alone is not, and that is the whole reason it works.
        #
        # Reading it the obvious way round does not: the death's own MOVE
        # frame lasts 0.02s against a 0.1s poll. So leaving the cart arms a
        # verdict, and a dismount is what cancels it.
        # This function is the only thing guaranteed to run on every poll, so
        # it is where the effect-hold deadline is kept alive. See on_coaster.
        if st == COASTER_STATE:
            self._coaster_until = time.time() + COASTER_GRACE
        if prev == COASTER_STATE and st != COASTER_STATE:
            self._coaster_left = time.time()
        if self._coaster_left:
            if st is None or loading:
                # He left the LEVEL, not the cart. Not a death of this kind.
                self._coaster_left = 0.0
            elif st == COASTER_STATE or st in COASTER_DISMOUNT:
                # Back on, or thrown clear -- either way the ride let him go.
                self._coaster_left = 0.0
            elif time.time() - self._coaster_left >= COASTER_VERDICT:
                self._coaster_left = 0.0
                return "void_out"

        # Dying as the mouse or the ball. A vanilla recording of the whole
        # cycle -- transform, die, transform again, break a poster, take the
        # teleporter -- says:
        #
        #   dying         BALL -> (request MOVE) -> MOVE, and he stays Taz
        #   the poster    BALL -> ... -> BALL, for fifty seconds, unchanged
        #   the teleport  BALL -> 0x31 INTRANSPORT -> MOVE
        #
        # So leaving the ball for MOVE really is the death. But firing on a
        # SINGLE reading of it turned breaking the Lab Poster into a spin,
        # which un-balls Taz while the game still thinks he is one -- a worse
        # soft lock than the one this exists to fix, and one that happens
        # whether or not the player has DeathLink on.
        #
        # So it has to hold, and the state object has to be the same one it
        # was. A pointer that moved means the object was rebuilt underneath
        # us and 0x00 is a half-built field, not a death.
        if lid == HAUNTED_LEVEL and prev in TRANSFORM_STATES \
                and st == 0x00 and not loading and obj == was_obj:
            self._transform_left = time.time()
        if self._transform_left:
            if st is None or loading or obj != was_obj \
                    or st in TRANSFORM_STATES or st == 0x31:
                # Back to a transform, into the teleporter, or the object
                # moved. None of those is a death.
                self._transform_left = 0.0
            elif st != 0x00:
                self._transform_left = 0.0
            elif time.time() - self._transform_left >= TRANSFORM_VERDICT:
                self._transform_left = 0.0
                # He stays the ball, model and all, and there is a pool of
                # water a few steps away -- so this is a soft lock waiting to
                # happen rather than a cosmetic oddity.
                self._unball_until = time.time() + self.UNBALL_FOR
                self._unball_said = False
                self.force_spin()
                return "void_out"

        if st is None or st == prev:
            return None
        if prev in (CAUGHT_STATE, DROWN_STATE, FALL_STATE, CRUSH_STATE,
                    VOID_STATE, BOSS_LOSS_STATE):
            # Leaving a death is not a new one.
            return None

        # In a boss arena the void is how the fight moves to its next phase,
        # so it is not a death at all. Only losing the fight is, and that has
        # its own detector.
        if lid in BOSS_LEVELS and st in (DROWN_STATE, FALL_STATE,
                                         CRUSH_STATE, VOID_STATE):
            return None


        if st == CAUGHT_STATE:
            self._net_at = time.time()
            return "captures"
        if st == CAGED_STATE and time.time() - self._net_at > NET_MEMORY:
            # On its own, so nothing netted him first: one of Taz: Haunted's
            # two cage catchers.
            return "captures"
        # Drowning is reported separately so the message can name it. Both
        # are void deaths as far as the yaml and the amnesty are concerned.
        if st == DROWN_STATE:
            return "drown"
        if st in (FALL_STATE, CRUSH_STATE, VOID_STATE):
            return "void_out"
        return None

    def teleport_to(self, pos):
        a = mem.deref(T.TAZ_PTR, 0xC0) if mem else None
        if a is None or not pos:
            return False
        try:
            mem.write_floats(a, tuple(pos))
        except Exception:
            return False
        # Ignore the jump we just caused, or receiving a DeathLink would send
        # one straight back out.
        self._self_move_until = time.time() + 3.0
        return True


def _gate_exit(pos, point, want):
    """Where to set Taz down so he ends `want` from a zone's centre.

    His height is kept -- shoving him vertically out of a doorway looks
    wrong -- so the horizontal leg has to cover whatever the vertical one
    does not. Scaling the whole 3D offset and then pinning Y back, which is
    what this used to do, lands him BACK INSIDE the radius whenever he is
    near the centre and even slightly off its height: 10 units across and 37
    up from Ice Burg's trigger came out at 222 from a 730 radius.
    """
    dy = pos[1] - point[1]
    leg = math.sqrt(max(want * want - dy * dy, 1.0))
    dh = math.hypot(pos[0] - point[0], pos[2] - point[2])
    if dh < 1.0:
        ux, uz = 1.0, 0.0
    else:
        ux, uz = (pos[0] - point[0]) / dh, (pos[2] - point[2]) / dh
    return (point[0] + ux * leg, pos[1], point[2] + uz * leg)


def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
