"""A flight recorder, so "it froze and I don't know why" is diagnosable.

PCSX2 going into permanent slow motion, or the game simply stopping, leaves
nothing behind: the player reloads a state and the evidence is gone. This
watches a handful of numbers every tick, writes them to a rolling log next to
the client's state file, and says something the moment one of them stops
behaving.

WHAT IT WATCHES, AND WHY EACH ONE
---------------------------------
All read-only. Nothing here writes to the game, ever.

  time scale   0x004125CC   The engine's live multiplier, read out of the
                            frame-time routine (loaded at 0x00285A20,
                            multiplied into the delta at 0x00285A28). Below
                            1.0 IS slow motion, whoever asked for it -- which
                            is exactly the symptom to catch. The game's own
                            slowdowns are short: a Golden Sam Statue was
                            recorded at 5.09s and a smashed poster at 2.91s,
                            and the West boss holds 0.5 for a phase. Anything
                            still slow after STUCK_SLOWMO is not the game.

  game time    0x003FF054   Advances while the game runs. If GAME_STATE says
                            Active and this stops moving, the game is frozen
                            even though the client is still being answered --
                            which is the difference between "PCSX2 hung" and
                            "PINE died", and they need different fixes.

  frame dt     0x00412664   The scaled delta 473 readers use. A dt that goes
                            to zero or explodes says the same thing from the
                            other side.

  game state   0x003FF040   1 = Active, 5 = loading. A long stretch in 5 is a
                            load that never finished.

  read errors               Consecutive failures reaching PINE. This is the
                            one fault where the log itself is the casualty,
                            so it is counted rather than sampled.

WHAT IT IS NOT
--------------
It cannot see a PCSX2 crash -- the process is gone and so is the socket. What
it CAN do is show the last few seconds before the socket went quiet, which is
the part nobody ever has when they report one.

The log is rolling and capped. It is meant to be attached to a bug report, so
it stays small enough to paste.
"""

import os
import time

TIME_SCALE = 0x004125CC
GAME_TIME = 0x003FF054
FRAME_DT = 0x00412664
GAME_STATE = 0x003FF040
LEVEL_ID = 0x003FF048

STATE_ACTIVE = 1
STATE_LOAD = 5

# The longest slowdown the game itself is known to run is the Golden Sam
# Statue at 5.09s. Three times that is comfortably past anything legitimate
# without crying wolf over a boss phase.
STUCK_SLOWMO = 15.0

# Game time not moving while the game says it is Active.
FROZEN_AFTER = 3.0

# A load that never finishes.
STUCK_LOAD = 60.0

# Consecutive failed reads before saying the emulator has gone.
LOST_AFTER = 20

# A cap, so a long session cannot fill a disk. Past it, samples stop but
# anomalies keep going -- the anomalies are the point.
MAX_LINES = 20000

# Where the logs go. Archipelago already has a logs folder; putting them
# anywhere else means they pile up in the root next to the launcher, which
# is where nobody wants them.
LOG_DIR = "logs"

# Say the same thing at most this often.
REPEAT_EVERY = 30.0

# A gap between ticks longer than this means the client itself stalled, or
# could not reach the emulator. Everything timed below is re-baselined across
# it: claiming the game was frozen for a stretch we were not watching is
# exactly the false positive that trains people to ignore a warning.
BLIND_TICK = 5.0


class Health:
    """Fed one sample per client tick. Returns lines worth telling a player."""

    def __init__(self, mem, path):
        self.mem = mem
        self.path = path
        self.lines = []
        self.written = 0
        self.errors = 0
        self.started = time.time()
        self._fh = None
        self._open()

        self._last_game_time = None
        self._game_time_moved_at = None
        self._slow_since = None
        self._load_since = None
        self._said = {}
        self._last_sample = None
        self._flushed = -1e9
        self._last_tick = None

    # -- the log -------------------------------------------------------
    def _open(self):
        """Fresh file per session, keeping the previous one.

        APPEND-AS-YOU-GO, not write-the-whole-thing-periodically. The first
        version rebuilt the file every twenty seconds, and a real PCSX2 crash
        proved why that is wrong: the log stopped 4.5s before the crash and
        the run-up -- the only part worth having -- was never on disk. A
        flight recorder that loses the last twenty seconds is not one.

        The previous session is kept as .prev.log, because the useful
        sequence is "it crashed, I restarted, here is the log".
        """
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            if os.path.exists(self.path):
                prev = self.path[:-4] + ".prev.log" \
                    if self.path.endswith(".log") else self.path + ".prev"
                try:
                    os.replace(self.path, prev)
                except Exception:
                    pass
            # buffering=1 is line buffered, so every line is on disk the
            # moment it is written. That is the whole point.
            self._fh = open(self.path, "w", encoding="utf-8", buffering=1)
            self._fh.write("Taz: Wanted -- client health log\n")
            self._fh.write("seconds since the client started, then what "
                           "changed\n\n")
        except Exception:
            self._fh = None

    def _log(self, text, always=False):
        line = "%8.1f  %s" % (time.time() - self.started, text)
        self.lines.append(line)
        if len(self.lines) > 400:            # a small tail, for tests
            del self.lines[:len(self.lines) - 400]
        if self.written >= MAX_LINES and not always:
            return
        self.written += 1
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
        except Exception:
            self._fh = None

    def flush(self):
        """Nothing to do -- every line is already on disk. Kept so callers
        that used to need it still work."""
        try:
            if self._fh is not None:
                self._fh.flush()
            return self._fh is not None
        except Exception:
            return False

    def close(self):
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None

    def _say(self, key, text, now):
        """One line per distinct problem, at most every REPEAT_EVERY."""
        if now - self._said.get(key, -1e9) < REPEAT_EVERY:
            return []
        self._said[key] = now
        self._log("*** " + text, always=True)
        self._flushed = now
        return [text]

    # -- one sample ----------------------------------------------------
    def _rebaseline(self, now, why):
        """Forget everything we were timing. We were not looking."""
        self._log("re-baselined: " + why)
        self._game_time_moved_at = now
        self._last_game_time = None
        self._slow_since = None
        self._load_since = None

    def tick(self, now=None):
        now = time.time() if now is None else now
        gap = None if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        mem = self.mem
        try:
            scale = mem.read_float(TIME_SCALE)
            gtime = mem.read_float(GAME_TIME)
            dt = mem.read_float(FRAME_DT)
            state = mem.read_u32(GAME_STATE)
            lid = mem.read_u32(LEVEL_ID)
        except Exception as exc:
            self.errors += 1
            if self.errors == LOST_AFTER:
                self._log("%d reads in a row failed (%s)"
                          % (self.errors, exc.__class__.__name__))
                return self._say(
                    "lost", "lost contact with PCSX2 -- if it crashed, the "
                    "health log next to your client state file has the last "
                    "few seconds before it went", now)
            return []
        if self.errors:
            n, self.errors = self.errors, 0
            self._rebaseline(now, "reads recovered after %d failures" % n)
        elif gap is not None and gap > BLIND_TICK:
            # The client itself stalled. Say so -- a player whose client is
            # being starved wants to know that, and it is the same class of
            # fault as the catcher judge going unpolled.
            self._rebaseline(now, "the client went %.1fs between looks" % gap)

        sample = (round(scale, 3), state, lid, round(dt, 4))
        if sample != self._last_sample:
            self._log("scale %.3f  dt %.4f  state %s  level %s  gametime %.1f"
                      % (scale, dt, state, state and lid, gtime))
            self._last_sample = sample

        out = []

        # --- is the game advancing at all? ---
        if gtime != self._last_game_time:
            self._last_game_time = gtime
            self._game_time_moved_at = now
        elif (state == STATE_ACTIVE and self._game_time_moved_at
                and now - self._game_time_moved_at > FROZEN_AFTER):
            out += self._say(
                "frozen",
                "the game says it is running but its clock has not moved for "
                "%.0fs -- PCSX2 is hung, not the client"
                % (now - self._game_time_moved_at), now)

        # --- stuck in slow motion ---
        if scale < 0.999:
            if self._slow_since is None:
                self._slow_since = now
                self._log("slow motion begins at %.3f" % scale)
            elif now - self._slow_since > STUCK_SLOWMO:
                out += self._say(
                    "slowmo",
                    "the engine has been in slow motion (%.2fx) for %.0fs. "
                    "The game's own slowdowns last about 5s, so this one is "
                    "stuck -- a save state reload clears it"
                    % (scale, now - self._slow_since), now)
        elif self._slow_since is not None:
            self._log("slow motion ends after %.2fs" % (now - self._slow_since))
            self._slow_since = None

        # --- a load that never finishes ---
        if state == STATE_LOAD:
            if self._load_since is None:
                self._load_since = now
            elif now - self._load_since > STUCK_LOAD:
                out += self._say(
                    "load",
                    "the game has been loading for %.0fs -- it is not going "
                    "to finish on its own" % (now - self._load_since), now)
        else:
            self._load_since = None

        return out

    def note(self, text):
        """Let the client drop its own events into the same timeline."""
        self._log(text)


def path_for(state_path):
    """logs/<seed>_health.log, rather than beside the launcher.

    Named from the client's state file so it is obvious which seed it
    belongs to, but placed in the logs folder so it is not underfoot.
    """
    base = os.path.basename(state_path)
    if base.endswith(".json"):
        base = base[:-5]
    return os.path.join(LOG_DIR, base + "_health.log")


def make(mem, state_path):
    try:
        h = Health(mem, path_for(state_path))
        h.note("client started")
        return h
    except Exception:
        return None


__all__ = ["Health", "make", "path_for", "STUCK_SLOWMO", "FROZEN_AFTER"]
